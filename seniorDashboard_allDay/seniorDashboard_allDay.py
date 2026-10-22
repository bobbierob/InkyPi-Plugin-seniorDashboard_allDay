from plugins.base_plugin.base_plugin import BasePlugin
from plugins.seniorDashboard_allDay.constants import CALENDAR_COLORS, LOCALE, LABELS, FONT_SIZES, WEATHER_ICONS
from plugins.seniorDashboard_allDay import reboot_manager
from plugins.seniorDashboard_allDay.reboot_manager import REBOOT_DELAY_SECONDS
from utils.app_utils import get_font
from PIL import ImageColor, Image, ImageDraw
import icalendar
import recurring_ical_events
import logging
import re
import requests
from datetime import date, datetime, timedelta
import pytz

logger = logging.getLogger(__name__)

# device.json key holding the last successfully fetched dashboard data (events + weather), used to
# keep showing a real dashboard when the network is down. Survives reboots.
CACHE_KEY = "seniorDashboard_allDay_cache"

# device.json key for the consecutive stale-refresh counter: how many refreshes in a row have shown
# cached (not freshly fetched) data since the last successful connection. Drives the corner
# indicator (green dot at 0, the number otherwise) and is reset on a successful fetch.
STALE_COUNT_KEY = "seniorDashboard_allDay_stale_count"

# Fallback weather coordinates (Darmstadt, DE) used when the settings map picker has no location set.
DEFAULT_WEATHER_LAT = 49.8728
DEFAULT_WEATHER_LON = 8.6512

# Rest-of-day rain view. Chance of rain is hourly; the timeline is drawn in half-hour cells.
RAIN_CHANCE_WET = 50  # an hour at or above this chance is drawn as rain on the timeline
RAIN_CHANCE_LOW = 20  # below this, with no expected amount, the rest of the day shows "No precipitation"
FREEZING_CODES = {56, 57, 66, 67}  # WMO: freezing drizzle / freezing rain -> red on the timeline + ice warning


class SeniorDashboardAllDay(BasePlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params['style_settings'] = True
        template_params['calendar_colors'] = CALENDAR_COLORS
        return template_params

    def generate_image(self, settings, device_config):
        """Always show a real dashboard. When online, fetch fresh data, cache it, and render with a
        green-dot indicator. When the fetch fails or the network is down, render the *same*
        dashboard from the cached data (correct current date + appointments + last weather) with a
        small number indicator instead, and -- if the network is genuinely down -- reboot a few
        minutes later to try to restore the Wi-Fi.

        Branches:
          1. No calendar configured  -> localized status screen, no reboot (a reboot can't add a URL).
          2. Online + fetch succeeds -> cache, reset counter, cancel reboot, dashboard + green dot.
          3. Online + fetch fails     -> re-check network; render cached dashboard + number.
                                         Reboot only if the network actually dropped mid-fetch.
          4. Offline                  -> render cached dashboard + number, reboot ~5 min later.
                                         (If no cache exists yet: localized status screen + reboot.)
        """
        calendar_urls = settings.get('calendarURLs[]')

        # 1. Configuration check.
        if not calendar_urls or not any((u or '').strip() for u in calendar_urls):
            logger.warning("seniorDashboard: no calendar URL configured")
            return self._handle_failure(device_config, settings, reason="config", urls=calendar_urls)

        # 2/3. Online path: try to fetch fresh data.
        if self._check_connectivity(calendar_urls):
            try:
                data = self._fetch_dashboard_data(settings, device_config, calendar_urls)
                image = self._render_from_data(
                    settings, device_config, data, indicator={"mode": "ok", "count": 0})
                # Only commit success once we have a rendered image in hand.
                self._save_cache(device_config, data)
                self._reset_stale_count(device_config)
                reboot_manager.cancel_reboot()
                return image
            except Exception:
                logger.exception("seniorDashboard: online update failed; falling back to cache")
                # A flap may have dropped the network mid-fetch -> re-classify.
                offline = not self._check_connectivity(calendar_urls)
                return self._render_stale_or_fallback(settings, device_config, offline=offline)

        # 4. Offline path: network is down.
        return self._render_stale_or_fallback(settings, device_config, offline=True)

    def _render_stale_or_fallback(self, settings, device_config, offline):
        """Render the dashboard from cached data with the stale-counter indicator. Falls back to the
        localized no-data status screen if no cache exists yet (a fresh install that has never
        connected). Schedules a recovery reboot only when the network is actually down."""
        cache = self._load_cache(device_config)
        count = self._increment_stale_count(device_config)

        if offline:
            tz = pytz.timezone(device_config.get_config("timezone", default="America/New_York"))
            reboot_manager.schedule_reboot(
                REBOOT_DELAY_SECONDS, datetime.now(tz) + timedelta(seconds=REBOOT_DELAY_SECONDS))

        if cache:
            try:
                image = self._render_from_data(
                    settings, device_config, cache, indicator={"mode": "stale", "count": count})
                if image:
                    return image
                logger.warning("seniorDashboard: cached render returned None; status fallback")
            except Exception:
                logger.exception("seniorDashboard: cached render failed; status fallback")

        # No usable cache (or cached render failed) -> bootstrap status screen.
        return self._handle_failure(
            device_config, settings,
            reason=("offline" if offline else "error"),
            urls=settings.get('calendarURLs[]'))

    # ----- Data fetch (network; online only) -----

    def _fetch_dashboard_data(self, settings, device_config, calendar_urls):
        """Fetch fresh calendar events + weather. Raises on any fetch error.

        Returns a JSON-serializable dict suitable for caching:
            {"events": [...], "weather": {...}, "fetched_at": "<ISO>"}
        The events are *unfiltered* within the ~2-week window; date-relative filtering happens at
        render time (_filter_active_events) so the cache stays reusable across multiple days.
        """
        calendar_colors = self._calendar_colors(settings, len(calendar_urls))

        timezone = device_config.get_config("timezone", default="America/New_York")
        units = settings.get("temperatureUnit") or "metric"
        latitude, longitude = self._get_weather_coords(settings)
        tz = pytz.timezone(timezone)

        current_dt = datetime.now(tz)
        start, end = self.get_view_range(current_dt)
        logger.info(f"Fetching events for this week and next week: {start} --> [{current_dt}] --> {end}")
        events = self.fetch_ics_events(calendar_urls, calendar_colors, tz, start, end, current_dt)
        if not events:
            logger.warning("No events found for ics url")

        # Weather is best-effort (fetch_weather_data catches its own errors and returns an empty block).
        # The weather API hiccups independently of the calendar (e.g. a transient 502). When it does,
        # don't blank the weather / clobber the cache -- reuse the last-known-good cached weather so a
        # weather outage never wipes the weather block while the calendar is still updating fine.
        weather_data = self.fetch_weather_data(timezone, units, latitude, longitude)
        if not (weather_data and weather_data.get("current")):
            prev = self._load_cache(device_config)
            prev_weather = (prev or {}).get("weather") or {}
            if prev_weather.get("current"):
                logger.info("seniorDashboard: weather fetch failed; reusing last cached weather")
                weather_data = prev_weather

        return {
            "events": events,
            "weather": weather_data,
            "fetched_at": current_dt.isoformat(),
        }

    # ----- Render (always; from fresh or cached data) -----

    def _render_from_data(self, settings, device_config, data, indicator):
        """Build the dashboard image from a data dict ({events, weather}) -- fresh or cached.

        Re-applies today-relative event filtering and day grouping against the *current* date, so
        cached events re-bucket correctly as days pass. Raises on render failure.
        """
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        timezone = device_config.get_config("timezone", default="America/New_York")
        time_format = device_config.get_config("time_format", default="12h")
        tz = pytz.timezone(timezone)
        current_dt = datetime.now(tz)

        events = self._filter_active_events(data.get("events") or [], current_dt, tz)
        weather_data = data.get("weather") or {}

        labels = LABELS
        # The list covers the whole fetched window; the template drops the days that don't fit.
        view_start, view_end = self.get_view_range(current_dt)
        colors = self._calendar_colors(settings, len(settings.get("calendarURLs[]") or []))
        agenda = self._build_agenda(events, current_dt, tz, (view_end - view_start).days, time_format, labels,
                                    colors if len(set(colors)) > 1 else None)

        template_params = {
            "title": f"{current_dt:%A}, {current_dt:%B} {current_dt.day}",
            "agenda": agenda,
            "plugin_settings": settings,
            "font_scale": FONT_SIZES.get(settings.get("fontSize", "normal")),
            "labels": labels,
            "weather": weather_data,
            "wx": self._weather_view(weather_data, data.get("fetched_at"), current_dt, tz),
            "rain": self._rain_view(weather_data, current_dt, time_format),
            "last_updated": self._format_last_updated(data.get("fetched_at"), current_dt, tz, time_format),
            "indicator": indicator,
        }

        image = self.render_image(dimensions, "seniorDashboard_allDay.html", "seniorDashboard_allDay.css", template_params)

        if not image:
            raise RuntimeError("Failed to take screenshot, please check logs.")
        return image

    @staticmethod
    def _weather_view(weather, fetched_at, now, tz):
        """Today's and tomorrow's weather for the current date, from fresh or cached data.

        Returns {"current", "today", "tomorrow"}:
          current:  the conditions at fetch time; only when the data was fetched today, since
                    yesterday's "current" temperature would be wrong.
          today:    today's daily forecast (icon, low/high), or None.
          tomorrow: tomorrow's daily forecast, or None once the data is too old to have one.
        A dashboard cached yesterday thus shows today's forecast instead of the current
        temperature, and its Tomorrow is the real tomorrow, not the day already on screen.
        """
        daily = list(weather.get("daily") or [])
        if not daily and weather.get("current"):
            # Data cached before daily entries had dates: today's low/high sat on "current" and
            # the one forecast entry was the day after. Date both from the fetch day.
            fetch_day = (weather.get("today_hourly") or {}).get("date")
            if not fetch_day and fetched_at:
                try:
                    fetch_day = datetime.fromisoformat(fetched_at).astimezone(tz).date().isoformat()
                except (TypeError, ValueError):
                    fetch_day = None
            if fetch_day:
                cur = weather["current"]
                daily.append({"date": fetch_day, "icon": cur.get("icon"),
                              "temp_min": cur.get("temp_min"), "temp_max": cur.get("temp_max")})
                for i, day in enumerate(weather.get("forecast") or [], start=1):
                    next_day = (date.fromisoformat(fetch_day) + timedelta(days=i)).isoformat()
                    daily.append(dict(day, date=next_day))

        by_date = {d.get("date"): d for d in daily}
        today = now.date()
        today_entry = by_date.get(today.isoformat())
        if today_entry and not (isinstance(today_entry.get("temp_min"), (int, float))
                                and isinstance(today_entry.get("temp_max"), (int, float))):
            today_entry = None
        fetched_today = bool(daily) and daily[0].get("date") == today.isoformat()
        return {
            "current": weather.get("current") if fetched_today else None,
            "today": today_entry,
            "tomorrow": by_date.get((today + timedelta(days=1)).isoformat()),
        }

    def _rain_view(self, weather, now, time_format):
        """Precipitation outlook from the current half-hour to midnight, for the weather panel.

        Computed at render time from the raw hourly data, so a dashboard re-rendered from cache
        later in the day still starts its timeline at the current time.

        Returns None when there is no hourly data for today (e.g. old cache, or cache from an
        earlier day). Otherwise a dict:
            state:    "wet" (something to draw on the timeline), "low", or "dry"
            lines:    summary lines, e.g. ["💧 80% · 4.6 mm"], ["❄️ 70% · 5 cm"], or for rain and
                      snow together ["🌨️ 60%", "💧 2 mm · ❄️ 3 cm"]
            icy:      True when freezing rain/drizzle is forecast for the rest of today
            segments: blocks as {"left", "width", "kind"} in % of the timeline (now -> midnight);
                      kind is "rain" (chance >= RAIN_CHANCE_WET) or "ice" (freezing precipitation)
            ticks:    time labels as {"left", "label"} in % of the timeline
        """
        hourly = (weather or {}).get("today_hourly") or {}
        if hourly.get("date") != now.date().isoformat():
            return None

        def num(key, hour):
            values = hourly.get(key) or []
            v = values[hour] if hour < len(values) else None
            return v if isinstance(v, (int, float)) else None

        def total(key):
            return sum(v for v in (num(key, h) for h in hours) if v is not None)

        hours = range(now.hour, 24)
        # The service sometimes returns no chance of rain (all null) while amounts and weather codes
        # are fine. Then the view falls back on the expected amounts and leaves the % off.
        chances = [c for c in (num("chance", h) for h in hours) if c is not None]
        max_chance = round(max(chances)) if chances else None
        if max_chance is None and all(num(k, h) is None for k in ("amount", "rain", "snow") for h in hours):
            return None
        imperial = (weather or {}).get("precipitation_unit") == "in"
        min_liquid, min_snow = (0.005, 0.05) if imperial else (0.1, 0.1)

        def likely(hour):
            """Is precipitation likely in this hour? By chance when known, else by expected amount."""
            chance = num("chance", hour)
            if chance is not None:
                return chance >= RAIN_CHANCE_WET
            return (num("amount", hour) or 0) >= min_liquid

        # Half-hour cells from the current half-hour to midnight, merged into same-kind blocks.
        # Freezing precipitation is marked whatever its chance: it is the safety-relevant case.
        start = (now.hour * 60 + now.minute) // 30 * 30  # minutes since midnight
        windows = []
        for slot in range(start, 24 * 60, 30):
            hour = slot // 60
            if num("code", hour) in FREEZING_CODES:
                kind = "ice"
            elif likely(hour):
                kind = "rain"
            else:
                continue
            if windows and windows[-1][1] == slot and windows[-1][2] == kind:
                windows[-1][1] = slot + 30
            else:
                windows.append([slot, slot + 30, kind])
        icy = any(kind == "ice" for _, _, kind in windows)

        # Summary line. Rain is liquid (mm / in); snow is depth (cm / in), so 5 cm reads as snow
        # rather than as "5 mm" of water. Data cached before the rain/snow split only has a total.
        liquid_unit, snow_unit = ("in", "in") if imperial else ("mm", "cm")
        has_split = "rain" in hourly and "snow" in hourly
        liquid = total("rain") if has_split else total("amount")
        snow = total("snow") if has_split else 0
        pct_text = f"{max_chance}% · " if max_chance is not None else ""
        both = (f"💧 {self._fmt_amount(liquid, imperial)} {liquid_unit}"
                f" · ❄️ {self._fmt_amount(snow, imperial)} {snow_unit}")
        if snow >= min_snow and liquid >= min_liquid:
            # Too long for one line in the weather column: chance first, then both amounts.
            lines = [f"🌨️ {max_chance}%", both] if max_chance is not None else [both]
        elif snow >= min_snow:
            lines = [f"❄️ {pct_text}{self._fmt_amount(snow, imperial)} {snow_unit}"]
        else:
            lines = [f"💧 {pct_text}{self._fmt_amount(liquid, imperial)} {liquid_unit}"]

        if windows:
            state = "wet"
        elif (max_chance or 0) >= RAIN_CHANCE_LOW or liquid > 0 or snow > 0:
            state = "low"
        else:
            state = "dry"

        span = 24 * 60 - start

        def pct(minutes):
            return round((minutes - start) / span * 100, 2)

        segments = [{"left": pct(a), "width": round((b - a) / span * 100, 2), "kind": kind}
                    for a, b, kind in windows]

        # Ticks every 3 hours (hourly when less than 6 hours remain), kept clear of the edges.
        step = 180 if span > 360 else 60
        ticks = []
        for m in range((start // step + 1) * step, 24 * 60, step):
            if 8 <= pct(m) <= 92:
                ticks.append({"left": pct(m), "label": self._tick_label(m, time_format)})

        return {"state": state, "lines": lines, "icy": icy, "segments": segments, "ticks": ticks}

    @staticmethod
    def _format_last_updated(fetched_at, now, tz, time_format):
        """When the shown data was last fetched: "2:05pm" / "14:05" today, with the month/day
        in front on an earlier day ("10/21 2:05pm"). None if unknown."""
        try:
            fetched = datetime.fromisoformat(fetched_at).astimezone(tz)
        except (TypeError, ValueError):
            return None
        clock = SeniorDashboardAllDay._clock(fetched, time_format)
        if fetched.date() == now.date():
            return clock
        return f"{fetched.month}/{fetched.day} {clock}"

    @staticmethod
    def _clock(dt, time_format):
        """A time of day as the dashboard writes it: "2:05pm", or "14:05" for the 24h setting."""
        if time_format == "24h":
            return dt.strftime("%H:%M")
        return f"{(dt.hour + 11) % 12 + 1}:{dt.minute:02d}{'am' if dt.hour < 12 else 'pm'}"

    @staticmethod
    def _fmt_amount(value, imperial):
        """1 decimal for mm/cm, 2 for inches, no trailing zeros: 4.6, 0, 12.3, 0.18."""
        return f"{round(value, 2 if imperial else 1):g}"

    @staticmethod
    def _tick_label(minutes, time_format):
        hour = minutes // 60
        if time_format == "24h":
            return f"{hour:02d}:00"
        return f"{(hour + 11) % 12 + 1}{'am' if hour < 12 else 'pm'}"

    def _build_agenda(self, events, current_dt, tz, days, time_format, labels, calendar_colors=None):
        """Group events into the days they fall on, for the calendar list.

        Returns today (always, with a placeholder when nothing is left today) and each later day
        in the window that has events:
            [{"is_today", "weekday" ("Fri"), "month_day" ("Oct 23"),
              "entries": [{"time", "title", "color"}]}]
        Multi-day events appear on every day they cover: all-day events as "All day"; timed ones
        as "from 11pm" on the first day, "All day" in between and "until 1am" on the last.
        Within a day, all-day items come first, then by start time.
        With calendar_colors (one per configured calendar; None when they'd all look the same),
        each entry carries its calendar's colour for the marker dot. The colour is looked up from
        the current settings, so a changed colour shows at once, even on cached events.
        """
        today = current_dt.date()
        by_day = {today + timedelta(days=i): [] for i in range(days)}
        for ev in events:
            try:
                start, end, last_date = self._event_span(ev, tz)
            except (ValueError, TypeError, AttributeError, KeyError):
                continue
            all_day = ev.get("allDay", False)
            title = ev.get("title") or ""
            color = None
            if calendar_colors:
                index = ev.get("calendar")
                color = (calendar_colors[index] if isinstance(index, int) and 0 <= index < len(calendar_colors)
                         else ev.get("backgroundColor"))
            day = max(start.date(), today)
            while day <= last_date and day in by_day:
                first, last = day == start.date(), day == last_date
                if all_day or (not first and not last):
                    time, sort_key = labels["allDayText"], (0, 0)
                elif first and (last or end is None):
                    time = self._event_clock(start, time_format)
                    if end is not None:
                        time += " - " + self._event_clock(end, time_format)
                    sort_key = (1, start.hour * 60 + start.minute)
                elif first:
                    time, sort_key = f"{labels['from']} {self._event_clock(start, time_format)}", (1, start.hour * 60 + start.minute)
                else:
                    time, sort_key = f"{labels['until']} {self._event_clock(end, time_format)}", (1, 0)
                by_day[day].append((sort_key, title, time, color))
                day += timedelta(days=1)

        agenda = []
        for day, items in by_day.items():
            if not items and day != today:
                continue
            items.sort(key=lambda item: (item[0], item[1].lower()))
            entries = [{"time": time, "title": title, "color": color} for _, title, time, color in items]
            if not entries:
                entries = [{"time": "", "title": labels["nothingMoreToday"], "placeholder": True}]
            agenda.append({
                "is_today": day == today,
                "weekday": f"{day:%a}",
                "month_day": f"{day:%b} {day.day}",
                "entries": entries,
            })
        return agenda

    @staticmethod
    def _calendar_colors(settings, count):
        """One marker colour per configured calendar: the colour picked in the settings, or the
        next unused colour from CALENDAR_COLORS for calendars without a valid one."""
        picked = settings.get("calendarColors[]") or []
        palette = [c for c, _ in CALENDAR_COLORS]
        colors = []
        for i in range(count):
            color = picked[i] if i < len(picked) else None
            if not (isinstance(color, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", color)):
                unused = [c for c in palette if c not in colors]
                color = (unused or palette)[0]
            colors.append(color)
        return colors

    @staticmethod
    def _event_clock(dt, time_format):
        """An event time as the calendar writes it: "2:30pm", "9am" (no ":00"), or "14:30"."""
        if time_format == "24h":
            return dt.strftime("%H:%M")
        hour = (dt.hour + 11) % 12 + 1
        minutes = f":{dt.minute:02d}" if dt.minute else ""
        return f"{hour}{minutes}{'am' if dt.hour < 12 else 'pm'}"

    def fetch_ics_events(self, calendar_urls, colors, tz, start_range, end_range, current_dt):
        """Fetch and parse all events in the [start_range, end_range] window (no date filtering).

        The ended-event filtering lives in _filter_active_events and runs at render time, so the
        returned (and cached) list stays reusable across multiple days of an offline outage.
        """
        parsed_events = []
        for index, (calendar_url, color) in enumerate(zip(calendar_urls, colors)):
            cal = self.fetch_calendar(calendar_url)
            events = recurring_ical_events.of(cal).between(start_range, end_range)
            contrast_color = self.get_contrast_color(color)

            for event in list(events):
                start, end, all_day = self.parse_data_points(event, tz)
                event_title = str(event.get('summary'))

                parsed_event = {
                    "title": event_title,
                    "calendar": index,
                    "start": start,
                    "backgroundColor": color,
                    "textColor": contrast_color,
                    "allDay": all_day
                }
                if end:
                    parsed_event['end'] = end

                parsed_events.append(parsed_event)

        logger.info(f"Parsed {len(parsed_events)} events in window")
        return parsed_events

    def _filter_active_events(self, events, current_dt, tz):
        """Drop events that have fully ended relative to current_dt. Keeps events whose date can't
        be parsed rather than hiding potentially valid data."""
        active = []
        for ev in events:
            try:
                start, end, last_date = self._event_span(ev, tz)
                # Ended before today.
                if last_date < current_dt.date():
                    continue
                # Timed event that has already ended.
                if not ev.get("allDay", False) and (end or start) <= current_dt:
                    continue
            except (ValueError, TypeError, AttributeError):
                pass  # keep on parse failure
            active.append(ev)
        return active

    @staticmethod
    def _event_span(ev, tz):
        """(start, end or None, last day the event is on) as timezone-aware values. End times are
        exclusive, as in iCal: an all-day event ending on the 24th is over by the 24th, and so is a
        timed event ending at midnight. Raises on unparseable dates."""
        def parse(value):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return tz.localize(dt) if dt.tzinfo is None else dt.astimezone(tz)

        start = parse(ev["start"])
        end = parse(ev["end"]) if ev.get("end") else None
        last_date = (end - timedelta(microseconds=1)).date() if end and end > start else start.date()
        return start, end, last_date

    def get_view_range(self, current_dt):
        """Get the date range for this week and next week (2 weeks total)."""
        start = datetime(current_dt.year, current_dt.month, current_dt.day)
        end = start + timedelta(weeks=2)
        return start, end

    def parse_data_points(self, event, tz):
        all_day = False
        dtstart = event.decoded("dtstart")
        if isinstance(dtstart, datetime):
            start = dtstart.astimezone(tz).isoformat()
        else:
            start = dtstart.isoformat()
            all_day = True

        end = None
        if "dtend" in event:
            dtend = event.decoded("dtend")
            if isinstance(dtend, datetime):
                end = dtend.astimezone(tz).isoformat()
            else:
                end = dtend.isoformat()
        elif "duration" in event:
            duration = event.decoded("duration")
            end = (dtstart + duration).isoformat()
        return start, end, all_day

    def _normalize_url(self, calendar_url):
        """Rewrite webcal:// URLs to https:// (requests cannot handle the webcal scheme)."""
        if calendar_url.startswith("webcal://"):
            return calendar_url.replace("webcal://", "https://")
        return calendar_url

    def _check_connectivity(self, calendar_urls):
        """Return True if at least one calendar URL is reachable over the network.

        Only a connection-level failure (timeout / connection refused / DNS) for *all* URLs
        counts as offline. If any URL returns an HTTP response -- even an error like 404/500 --
        the network is up and we treat it as online (a reboot would not fix a server error).
        """
        for url in calendar_urls:
            if not url or not url.strip():
                continue
            try:
                # stream=True avoids downloading the body; we only care that bytes start flowing.
                response = requests.get(self._normalize_url(url.strip()), timeout=5, stream=True)
                response.close()
                return True  # got an HTTP response => network is reachable
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                logger.warning(f"Calendar URL unreachable (network): {url} ({e})")
                continue
            except requests.exceptions.RequestException as e:
                # Any other request error still means the server answered/resolved => online.
                logger.warning(f"Calendar URL reachable but errored: {url} ({e})")
                return True
        return False

    # ----- Local state: cached data + stale counter (persisted in device.json) -----

    def _save_cache(self, device_config, data):
        try:
            device_config.update_value(CACHE_KEY, data, write=True)
        except Exception:
            logger.warning("seniorDashboard: could not persist data cache", exc_info=True)

    def _load_cache(self, device_config):
        """Return the cached data dict ({events, weather, ...}) or None if no usable cache exists."""
        try:
            cache = device_config.get_config(CACHE_KEY, default=None)
            if isinstance(cache, dict) and cache.get("events") is not None:
                return cache
        except Exception:
            logger.warning("seniorDashboard: could not read data cache", exc_info=True)
        return None

    def _get_stale_count(self, device_config):
        try:
            return int(device_config.get_config(STALE_COUNT_KEY, default=0) or 0)
        except Exception:
            return 0

    def _set_stale_count(self, device_config, value):
        try:
            device_config.update_value(STALE_COUNT_KEY, int(value), write=True)
        except Exception:
            logger.warning("seniorDashboard: could not persist stale count", exc_info=True)

    def _increment_stale_count(self, device_config):
        count = self._get_stale_count(device_config) + 1
        self._set_stale_count(device_config, count)
        return count

    def _reset_stale_count(self, device_config):
        if self._get_stale_count(device_config) != 0:
            self._set_stale_count(device_config, 0)

    # ----- No-data status screen (used only when there is no cached dashboard to show) -----

    def _format_reboot_time(self, device_config, reboot_at, labels):
        """Format a reboot time the way the dashboard writes times ("1:40pm" / "13:40")."""
        timezone = device_config.get_config("timezone", default="America/New_York")
        time_format = device_config.get_config("time_format", default="12h")
        return self._clock(reboot_at.astimezone(pytz.timezone(timezone)), time_format)

    def _handle_failure(self, device_config, settings, reason, urls):
        """Render a localized no-data status screen (never raises). Used only when there is no cached
        dashboard to show: reason 'config' (no calendar configured -> no reboot) or the
        'offline'/'error' bootstrap before the first successful fetch. Reboot scheduling for the
        offline bootstrap case is handled by the caller; here we only display any pending reboot time.
        """
        try:
            labels = LABELS

            # Config errors are not recoverable by a reboot.
            if reason == "config":
                return self._render_status_image(
                    settings, device_config,
                    title=labels.get("errorTitle", "Error"),
                    message=labels.get("configMessage", ""),
                    reboot_prefix=None, reboot_time=None, note=None)

            # Show the pending reboot time if the caller scheduled one.
            pending = reboot_manager.get_scheduled_reboot()
            reboot_time_str = self._format_reboot_time(device_config, pending, labels) if pending else None

            if reason == "offline":
                title = labels.get("offlineTitle", "No internet connection")
                message = None
                reboot_prefix = labels.get("offlineMessage")
            else:  # error
                title = labels.get("errorTitle", "Update failed")
                message = labels.get("errorMessage")
                reboot_prefix = labels.get("rebootPrefix")

            if not reboot_time_str:
                reboot_prefix = None
            note = labels.get("offlineReassure")

            return self._render_status_image(
                settings, device_config,
                title=title, message=message,
                reboot_prefix=reboot_prefix, reboot_time=reboot_time_str, note=note)
        except Exception:
            logger.exception("seniorDashboard: failure handler crashed; emergency PIL screen")
            try:
                return self._render_status_pil(device_config, "Error", None, None, None, None)
            except Exception:
                w, h = device_config.get_resolution()
                return Image.new("RGB", (w, h), "white")

    def _render_status_image(self, settings, device_config, title, message,
                             reboot_prefix, reboot_time, note):
        """Render the status/error screen via HTML; fall back to pure PIL if Chromium fails."""
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
        timezone = device_config.get_config("timezone", default="America/New_York")
        locale_code = LOCALE
        tz = pytz.timezone(timezone)

        template_params = {
            "current_dt": datetime.now(tz).isoformat(),
            "timezone": timezone,
            "locale_code": locale_code,
            "title": title,
            "message": message,
            "reboot_prefix": reboot_prefix,
            "reboot_time": reboot_time,
            "note": note,
            "font_scale": FONT_SIZES.get(settings.get("fontSize", "normal")),
            "plugin_settings": settings,
        }
        try:
            image = self.render_image(dimensions, "offline.html", "offline.css", template_params)
            if image:
                return image
            logger.warning("seniorDashboard: status render returned None; PIL fallback")
        except Exception:
            logger.exception("seniorDashboard: status HTML render failed; PIL fallback")
        return self._render_status_pil(device_config, title, message, reboot_prefix, reboot_time, note)

    def _render_status_pil(self, device_config, title, message, reboot_prefix, reboot_time, note):
        """Last-resort status screen drawn with PIL only (no Chromium dependency)."""
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
        w, h = dimensions
        tz = pytz.timezone(device_config.get_config("timezone", default="America/New_York"))
        now = datetime.now(tz)

        img = Image.new("RGB", (w, h), "white")
        draw = ImageDraw.Draw(img)

        # (text, y-fraction, font-size-fraction, weight, color); skip empty lines.
        reboot_line = None
        if reboot_time:
            reboot_line = f"{reboot_prefix} {reboot_time}".strip() if reboot_prefix else reboot_time
        rows = [
            (now.strftime("%d.%m.%Y"), 0.16, 0.085, "bold", (0, 0, 0)),
            (title, 0.36, 0.075, "bold", (200, 0, 0)),
            (message, 0.50, 0.050, "normal", (0, 0, 0)),
            (reboot_line, 0.64, 0.060, "bold", (0, 0, 200)),
            (note, 0.80, 0.045, "normal", (0, 0, 0)),
        ]
        for text, yf, sf, weight, color in rows:
            if not text:
                continue
            try:
                font = get_font("Jost", int(h * sf), weight)
                draw.text((w / 2, h * yf), text, font=font, fill=color, anchor="mm")
            except Exception:
                logger.warning("seniorDashboard: PIL draw failed for a line", exc_info=True)
        return img

    def fetch_calendar(self, calendar_url):
        calendar_url = self._normalize_url(calendar_url)
        try:
            response = requests.get(calendar_url, timeout=30)
            response.raise_for_status()
            return icalendar.Calendar.from_ical(response.text)
        except Exception as e:
            raise RuntimeError(f"Failed to fetch iCalendar url: {str(e)}")

    def get_contrast_color(self, color):
        """
        Returns '#000000' (black) or '#ffffff' (white) depending on the contrast
        against the given color.
        """
        r, g, b = ImageColor.getrgb(color)
        # YIQ formula to estimate brightness
        yiq = (r * 299 + g * 587 + b * 114) / 1000

        return '#000000' if yiq >= 150 else '#ffffff'

    def get_weather_icon(self, code):
        """Get weather icon emoji for a given weather code."""
        return WEATHER_ICONS.get(code, "❓")

    def _get_weather_coords(self, settings):
        """Return the (latitude, longitude) for the weather fetch from the settings the map picker
        writes. Falls back to the default Darmstadt coordinates when unset or unparseable."""
        lat, lon = DEFAULT_WEATHER_LAT, DEFAULT_WEATHER_LON
        try:
            if (settings.get("latitude") or "").strip() != "":
                lat = float(settings["latitude"])
            if (settings.get("longitude") or "").strip() != "":
                lon = float(settings["longitude"])
        except (TypeError, ValueError):
            logger.warning("seniorDashboard: invalid weather coordinates in settings; using defaults")
            return DEFAULT_WEATHER_LAT, DEFAULT_WEATHER_LON
        return lat, lon

    def fetch_weather_data(self, timezone, units="metric",
                           latitude=DEFAULT_WEATHER_LAT, longitude=DEFAULT_WEATHER_LON):
        """Fetch weather data from the Open-Meteo API.

        `units` selects the temperature unit: "imperial" (°F) or "metric" (°C, default).
        The API converts the values for us; we stamp the matching symbol into the returned
        dict so cached renders never mismatch values and label.
        `latitude`/`longitude` come from the settings map picker (default: Darmstadt).
        """
        # Open-Meteo's default forecast: it picks the best weather models for the location
        # (for Toronto: Canada's 2.5 km HRDPS, the hourly US HRRR, and global models further out).
        URL = "https://api.open-meteo.com/v1/forecast"

        # Open-Meteo temperature/precipitation unit params + display symbols, keyed by the settings value.
        if units == "imperial":
            api_unit, unit_symbol, precip_api_unit, precip_symbol = "fahrenheit", "°F", "inch", "in"
        else:
            api_unit, unit_symbol, precip_api_unit, precip_symbol = "celsius", "°C", "mm", "mm"

        params = {
            "latitude": latitude,
            "longitude": longitude,
            "current_weather": True,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode",
            "hourly": "precipitation,precipitation_probability,rain,showers,snowfall,weather_code",
            # Today, tomorrow and the day after, so a cache saved yesterday still has a tomorrow.
            "forecast_days": 3,
            "temperature_unit": api_unit,
            "precipitation_unit": precip_api_unit,
            "timezone": timezone
        }

        try:
            response = requests.get(URL, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            # Daily arrays start at *today*: index 0 = today, index 1 = tomorrow, ...
            daily = data.get("daily", {})

            def daily_value(key, i, default=0):
                values = daily.get(key) or []
                return values[i] if i < len(values) else default

            # Current weather, as of the fetch.
            current = data.get("current_weather", {})
            current_weather = {
                "icon": self.get_weather_icon(current.get("weathercode", 0)),
                "temperature": current.get("temperature", 0),
                "windspeed": current.get("windspeed", 0),
                "weathercode": current.get("weathercode", 0),
            }

            # Today's hourly precipitation, indexed by local hour 0-23 (times are in `timezone`).
            # Kept raw so the rest-of-day view can be recomputed at render time, also from cache.
            #   chance: % chance of any precipitation      amount: total, as liquid (mm / in)
            #   rain:   rain + showers, liquid (mm / in)   snow:   snowfall depth (cm / in)
            #   code:   WMO weather code (used to spot freezing rain / drizzle)
            hourly = data.get("hourly", {})
            today_str = (daily.get("time") or [None])[0]
            fields = {"chance": "precipitation_probability", "amount": "precipitation",
                      "rain": "rain", "showers": "showers", "snow": "snowfall", "code": "weather_code"}
            series = {key: [None] * 24 for key in fields}
            for i, t in enumerate(hourly.get("time") or []):
                if today_str and t.startswith(today_str):
                    hour = int(t[11:13])
                    for key, api_name in fields.items():
                        values = hourly.get(api_name) or []
                        series[key][hour] = values[i] if i < len(values) else None
            # Showers are rain too; fold them in so "rain" is all liquid precipitation.
            series["rain"] = [None if r is None and s is None else (r or 0) + (s or 0)
                              for r, s in zip(series["rain"], series.pop("showers"))]
            today_hourly = {"date": today_str, **series}

            # Daily forecast by date ("YYYY-MM-DD"). The render picks today's low/high and
            # tomorrow's forecast by date, so cached data stays right as the days pass.
            forecast_days = [{
                "date": day,
                "icon": self.get_weather_icon(daily_value("weathercode", i)),
                "temp_min": daily_value("temperature_2m_min", i, None),
                "temp_max": daily_value("temperature_2m_max", i, None),
                "precipitation": daily_value("precipitation_sum", i),
                "weathercode": daily_value("weathercode", i),
            } for i, day in enumerate(daily.get("time") or [])]

            return {
                "current": current_weather,
                "daily": forecast_days,
                "temperature_unit": unit_symbol,
                "precipitation_unit": precip_symbol,
                "today_hourly": today_hourly
            }
        except Exception as e:
            logger.warning(f"Failed to fetch weather data: {str(e)}")
            return {
                "current": None,
                "forecast": []
            }
