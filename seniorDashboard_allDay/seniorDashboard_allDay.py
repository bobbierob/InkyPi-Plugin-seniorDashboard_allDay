from plugins.base_plugin.base_plugin import BasePlugin
from plugins.seniorDashboard_allDay.constants import LOCALE_MAP, LABELS, FONT_SIZES, WEATHER_ICONS
from plugins.seniorDashboard_allDay import reboot_manager
from plugins.seniorDashboard_allDay.reboot_manager import REBOOT_DELAY_SECONDS, MAX_CONSECUTIVE_REBOOTS
from utils.app_utils import get_font
from PIL import ImageColor, Image, ImageDraw
import icalendar
import recurring_ical_events
import logging
import requests
from datetime import datetime, timedelta
import pytz

logger = logging.getLogger(__name__)

# device.json key for the persisted consecutive-reboot counter (survives reboots).
FAILCOUNT_KEY = "seniorDashboard_allDay_reboot_count"


class SeniorDashboardAllDay(BasePlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params['style_settings'] = True
        template_params['locale_map'] = LOCALE_MAP
        return template_params

    def generate_image(self, settings, device_config):
        """Never fail silently. Always return an image: the dashboard when all is well, or a
        localized status/error screen (with the current date) otherwise -- and schedule an
        auto-reboot to recover, bounded by a consecutive-reboot cap.

        Failure handling is layered:
          1. Missing calendar config -> error screen, no reboot (a reboot can't add a URL).
          2. Network unreachable      -> offline screen + reboot (the dead-WLAN case).
          3. Any other failure (calendar HTTP/parse error, render glitch, ...) -> error screen;
             reboot only if the network is actually down, else still show the error.
        """
        calendar_urls = settings.get('calendarURLs[]')
        try:
            # 1. Configuration check
            if not calendar_urls or not any((u or '').strip() for u in calendar_urls):
                logger.warning("seniorDashboard: no calendar URL configured")
                return self._handle_failure(device_config, settings, reason="config", urls=calendar_urls)

            # 2. Network reachability gate
            if not self._check_connectivity(calendar_urls):
                return self._handle_failure(device_config, settings, reason="offline", urls=calendar_urls)

            # 3. Online: build the dashboard
            image = self._render_dashboard(settings, device_config, calendar_urls)
            if not image:
                raise RuntimeError("Failed to take screenshot, please check logs.")

            # Success: clear any pending reboot and reset the failure counter.
            reboot_manager.cancel_reboot()
            self._reset_failure_count(device_config)
            return image
        except Exception:
            logger.exception("seniorDashboard: update failed; showing error screen")
            return self._handle_failure(device_config, settings, reason="error", urls=calendar_urls)

    def _render_dashboard(self, settings, device_config, calendar_urls):
        """Build the normal dashboard image (raises on any fetch/render problem)."""
        calendar_colors = settings.get('calendarColors[]')
        default_color = '#007BFF'
        if not calendar_colors or len(calendar_colors) < len(calendar_urls):
            calendar_colors = [default_color] * len(calendar_urls)

        view = "listWeek"  # Fixed to list view (today + next 2 days)
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
        
        timezone = device_config.get_config("timezone", default="America/New_York")
        time_format = device_config.get_config("time_format", default="12h")
        tz = pytz.timezone(timezone)

        current_dt = datetime.now(tz)
        start, end = self.get_view_range(current_dt)
        print(f"\n{'='*80}")
        print(f"[SeniorDashboard] Current time: {current_dt}")
        print(f"[SeniorDashboard] Fetching events from {start} to {end}")
        print(f"{'='*80}\n")
        logger.info(f"Fetching events for this week and next week: {start} --> [{current_dt}] --> {end}")
        events = self.fetch_ics_events(calendar_urls, calendar_colors, tz, start, end, current_dt)
        if not events:
            logger.warn("No events found for ics url")

        # Hardcode display options to True
        display_settings = settings.copy()
        display_settings["displayTitle"] = "true"
        display_settings["displayWeekends"] = "true"
        display_settings["displayEventTime"] = "true"
        
        # Ensure language is set (default to 'en' if not provided)
        if "language" not in display_settings or not display_settings["language"]:
            display_settings["language"] = "en"
        
        # Get locale for date formatting and labels
        locale_code = display_settings.get("language", "en")
        labels = LABELS.get(locale_code, LABELS["en"])

        # If no events (anymore) for today, add placeholder so today section is never dropped
        has_today_event = self._has_event_on_date(events, current_dt.date(), tz)
        if not has_today_event:
            today_start = current_dt.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            placeholder = {
                "title": labels["nothingMoreToday"],
                "start": today_start,
                "allDay": True,
                "backgroundColor": default_color,
                "textColor": self.get_contrast_color(default_color),
                "classNames": ["senior-dashboard-nothing-more"],
            }
            events = list(events) + [placeholder]

        # If no events for tomorrow, add placeholder with noEventsContent (no time/all-day shown)
        tomorrow_date = current_dt.date() + timedelta(days=1)
        if not self._has_event_on_date(events, tomorrow_date, tz):
            tomorrow_start = (current_dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            events = list(events) + [{
                "title": labels["noEventsContent"],
                "start": tomorrow_start,
                "allDay": True,
                "backgroundColor": default_color,
                "textColor": self.get_contrast_color(default_color),
                "classNames": ["senior-dashboard-nothing-more"],
            }]

        # If no events for day after tomorrow, add placeholder with noEventsContent (no time/all-day shown)
        day_after_date = current_dt.date() + timedelta(days=2)
        if not self._has_event_on_date(events, day_after_date, tz):
            day_after_start = (current_dt + timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            events = list(events) + [{
                "title": labels["noEventsContent"],
                "start": day_after_start,
                "allDay": True,
                "backgroundColor": default_color,
                "textColor": self.get_contrast_color(default_color),
                "classNames": ["senior-dashboard-nothing-more"],
            }]

        # Fetch weather data (uses locale for forecast day labels)
        weather_data = self.fetch_weather_data(timezone, locale_code)

        template_params = {
            "view": view,
            "events": events,
            "current_dt": current_dt.replace(minute=0, second=0, microsecond=0).isoformat(),
            "timezone": timezone,
            "plugin_settings": display_settings,
            "time_format": time_format,
            "font_scale": FONT_SIZES.get(settings.get("fontSize", "normal")),
            "locale_code": locale_code,
            "labels": labels,
            "weather": weather_data
        }

        image = self.render_image(dimensions, "seniorDashboard_allDay.html", "seniorDashboard_allDay.css", template_params)

        if not image:
            raise RuntimeError("Failed to take screenshot, please check logs.")
        return image
    
    def fetch_ics_events(self, calendar_urls, colors, tz, start_range, end_range, current_dt):
        parsed_events = []
        # Use start of current day for filtering (not current time)
        current_day_start = current_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        
        print(f"[SeniorDashboard] Current day start for filtering: {current_day_start}")

        for calendar_url, color in zip(calendar_urls, colors):
            cal = self.fetch_calendar(calendar_url)
            events = recurring_ical_events.of(cal).between(start_range, end_range)
            contrast_color = self.get_contrast_color(color)
            
            events_list = list(events)
            print(f"[SeniorDashboard] Fetched {len(events_list)} events from calendar")
            
            event_num = 0
            for event in events_list:
                event_num += 1
                start, end, all_day = self.parse_data_points(event, tz)
                event_title = str(event.get('summary'))
                print(f"[SeniorDashboard] Event #{event_num}: '{event_title}'")
                print(f"                  Start: {start} | End: {end} | All-day: {all_day}")
                
                # Filter out events that have fully ended (before today, or already ended today)
                try:
                    # Use end time if available, otherwise start time
                    end_iso = end or start
                    end_dt = datetime.fromisoformat(end_iso)
                    
                    # Make naive datetime timezone-aware if needed for comparison
                    if end_dt.tzinfo is None:
                        end_dt = tz.localize(end_dt)
                    
                    # Filter out events that ended before today
                    if end_dt.date() < current_day_start.date():
                        print(f"                  ❌ FILTERED OUT (ended {end_dt.date()} < {current_day_start.date()})")
                        continue
                    # Filter out today's timed events that have already ended (end time in the past)
                    if end_dt.date() == current_dt.date() and not all_day and end_dt <= current_dt:
                        print(f"                  ❌ FILTERED OUT (today's event already ended at {end_dt})")
                        continue
                    print(f"                  ✅ INCLUDED (ended {end_dt.date()} >= {current_day_start.date()})")
                except Exception as e:
                    # If parsing fails, keep the event to avoid hiding valid data
                    print(f"                  ⚠️  Error parsing, keeping event: {e}")
                    pass

                parsed_event = {
                    "title": event_title,
                    "start": start,
                    "backgroundColor": color,
                    "textColor": contrast_color,
                    "allDay": all_day
                }
                if end:
                    parsed_event['end'] = end

                parsed_events.append(parsed_event)
        
        print(f"\n[SeniorDashboard] Total events after filtering: {len(parsed_events)}")
        print(f"{'='*80}\n")
        return parsed_events
    
    def _has_event_on_date(self, events, target_date, tz):
        """Return True if any event starts on target_date (timezone-aware comparison)."""
        for ev in events:
            start_str = ev.get("start")
            if not start_str:
                continue
            try:
                dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = tz.localize(dt)
                else:
                    dt = dt.astimezone(tz)
                if dt.date() == target_date:
                    return True
            except (ValueError, TypeError):
                continue
        return False

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

    # ----- Failure handling (never raises; always returns an image) -----

    def _get_failure_count(self, device_config):
        try:
            return int(device_config.get_config(FAILCOUNT_KEY, default=0) or 0)
        except Exception:
            return 0

    def _set_failure_count(self, device_config, value):
        try:
            device_config.update_value(FAILCOUNT_KEY, int(value), write=True)
        except Exception:
            logger.warning("seniorDashboard: could not persist reboot count", exc_info=True)

    def _reset_failure_count(self, device_config):
        if self._get_failure_count(device_config) != 0:
            self._set_failure_count(device_config, 0)

    def _format_reboot_time(self, device_config, reboot_at, labels):
        """Format a reboot time as e.g. '13:40 Uhr' / '1:40 PM', honoring 12h/24h + locale clock suffix."""
        timezone = device_config.get_config("timezone", default="America/New_York")
        time_format = device_config.get_config("time_format", default="12h")
        local = reboot_at.astimezone(pytz.timezone(timezone))
        if time_format == "12h":
            s = local.strftime("%I:%M %p").lstrip("0")
        else:
            s = local.strftime("%H:%M")
        return f"{s} {labels.get('offlineClock', '')}".strip()

    def _handle_failure(self, device_config, settings, reason, urls):
        """Render a localized status/error screen and (per policy) schedule a capped reboot.

        reason: 'config' (no calendar configured -> never reboot),
                'offline' (network down -> reboot), or
                'error' (any other failure -> reboot only if the network is actually down now).
        Must never raise: falls back to a pure-PIL screen, then to a blank image.
        """
        try:
            locale_code = settings.get("language") or "en"
            labels = LABELS.get(locale_code, LABELS["en"])

            # Config errors are not recoverable by a reboot.
            if reason == "config":
                return self._render_status_image(
                    settings, device_config,
                    title=labels.get("errorTitle", "Error"),
                    message=labels.get("configMessage", ""),
                    reboot_prefix=None, reboot_time=None, note=None)

            # An "error" may actually be a network drop mid-update -> reclassify as offline.
            if reason == "error":
                try:
                    if urls and not self._check_connectivity(urls):
                        reason = "offline"
                except Exception:
                    pass

            # Reboot decision, bounded by the consecutive-reboot cap.
            reboot_time_str = None
            pending = reboot_manager.get_scheduled_reboot()
            if pending is not None:
                # Already scheduled in this process -> keep the same time (stable display).
                reboot_time_str = self._format_reboot_time(device_config, pending, labels)
            else:
                count = self._get_failure_count(device_config)
                if count < MAX_CONSECUTIVE_REBOOTS:
                    self._set_failure_count(device_config, count + 1)
                    tz = pytz.timezone(device_config.get_config("timezone", default="America/New_York"))
                    reboot_at = reboot_manager.schedule_reboot(
                        REBOOT_DELAY_SECONDS, datetime.now(tz) + timedelta(seconds=REBOOT_DELAY_SECONDS))
                    reboot_time_str = self._format_reboot_time(device_config, reboot_at, labels)
                else:
                    reboot_manager.cancel_reboot()
                    logger.warning(
                        "seniorDashboard: reboot cap (%d) reached -> not rebooting, just showing screen",
                        MAX_CONSECUTIVE_REBOOTS)

            # Pick localized strings for the chosen reason.
            if reason == "offline":
                title = labels.get("offlineTitle", "No internet connection")
                message = None
                reboot_prefix = labels.get("offlineMessage")
            else:  # error
                title = labels.get("errorTitle", "Update failed")
                message = labels.get("errorMessage")
                reboot_prefix = labels.get("rebootPrefix")

            if reboot_time_str:
                note = labels.get("offlineReassure")
            else:
                # Cap reached: drop the reboot line, show the persistent-problem note instead.
                reboot_prefix = None
                note = labels.get("noRebootNote")

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
        locale_code = settings.get("language") or "en"
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

    def fetch_weather_data(self, timezone, locale_code="en"):
        """Fetch weather data from Open-Meteo API."""
        URL = "https://api.open-meteo.com/v1/dwd-icon"
        day_labels = LABELS.get(locale_code, LABELS["en"])

        # Default coordinates (can be made configurable later)
        params = {
            "latitude": 49.8728,
            "longitude": 8.6512,
            "current_weather": True,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode",
            "forecast_days": 3,
            "timezone": timezone
        }
        
        try:
            response = requests.get(URL, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            # Process current weather
            current = data.get("current_weather", {})
            current_weather = {
                "icon": self.get_weather_icon(current.get("weathercode", 0)),
                "temperature": current.get("temperature", 0),
                "windspeed": current.get("windspeed", 0),
                "weathercode": current.get("weathercode", 0)
            }
            
            # Process daily forecast (first 2 days: tomorrow and day after)
            daily = data.get("daily", {})
            forecast = []
            if "time" in daily:
                for i, day in enumerate(daily["time"][:2]):
                    date_label = day_labels["tomorrow"] if i == 0 else day_labels["dayAfterTomorrow"]
                    forecast.append({
                        "date": date_label,
                        "icon": self.get_weather_icon(daily.get("weathercode", [0])[i] if i < len(daily.get("weathercode", [])) else 0),
                        "temp_min": daily.get("temperature_2m_min", [0])[i] if i < len(daily.get("temperature_2m_min", [])) else 0,
                        "temp_max": daily.get("temperature_2m_max", [0])[i] if i < len(daily.get("temperature_2m_max", [])) else 0,
                        "precipitation": daily.get("precipitation_sum", [0])[i] if i < len(daily.get("precipitation_sum", [])) else 0,
                        "weathercode": daily.get("weathercode", [0])[i] if i < len(daily.get("weathercode", [])) else 0
                    })
            
            return {
                "current": current_weather,
                "forecast": forecast
            }
        except Exception as e:
            logger.warning(f"Failed to fetch weather data: {str(e)}")
            return {
                "current": None,
                "forecast": []
            }
