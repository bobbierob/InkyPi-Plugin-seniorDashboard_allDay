# InkyPi-Plugin-seniorDashboard_allDay
![Example of InkyPi-Plugin-seniorDashboard_allDay](./example.png)

*InkyPi-Plugin-seniorDashboard_allDay* is a plugin for [InkyPi](https://github.com/fatihak/InkyPi) that shows a simple, at-a-glance view of the next few days: a calendar list and a small weather block. It is intended for an elderly person who has a calendar maintained for them by a family member or carer.

**What it does:**

- **Calendar** — Displays today and the next two of days in a list. Events that have already ended are hidden. 
- **Weather** — Shows current conditions and a short forecast (e.g. tomorrow and the day after) in a minimal layout: icon and temperature, with high/low for the next days.
It uses the DWD (Deutscher Wetter Dienst) free API, so no API-key config needed.
- **Never fails silently — error handling & auto-recovery** — The plugin always shows *something* useful: the dashboard when all is well, otherwise a clear, localized screen with the current date and what went wrong. If the cause is a lost internet connection it reboots itself automatically to recover (bounded, so it never reboot-loops). See [Error handling & auto-reboot](#error-handling--auto-reboot) below.

The layout is kept clear and low-clutter so it works well on an e-ink display and for quick, easy reading.

It is optimized for and tested only on landscape waveshare 7.2 inch display but should render ok on every comparable landscape display.

It's basically a mix of the calendar list view and weather template.
No additional dependencies whatsoever.

**Settings:**
![Screenshot of settings of InkyPi-Plugin-seniorDashboard_allDay](./settings.png)
Language can be set to **English**, **French** , **Spanish**  or **German**. 
Other languages can easily be added in *constants.py*, just make sure to use the correct international language ID so the calendar returns correct dates/formats.
You can add multiple calendars, they will all be used to compile the today, tomorrow and day after tomorrow list.
Location setting for the weather (DWD supplies world wide weather info).
Font size for the Calendar listing.


## Error handling & auto-reboot

This plugin is built for an unattended display that someone depends on every day. A common real-world problem is that the Raspberry Pi or the router drops the Wi-Fi connection (often due to power-save), the calendar can no longer be fetched, and the display **quietly keeps showing stale information** — which is exactly what you don't want for someone who relies on it to know the date and their appointments. Other things can go wrong too (a calendar server error, malformed calendar data, a rendering hiccup), and any of them could otherwise leave the screen silently stuck.

**The guarantee: the plugin never fails silently.** Every refresh either shows the normal dashboard or a clear, fully localized screen that always includes **today's date** and what happened. When the problem looks recoverable, the device reboots itself to try to fix it — but in a bounded way that can't turn into an endless reboot loop.

On each refresh:

- **Everything OK** → normal dashboard. (Any reboot scheduled by a previous failed refresh is automatically cancelled, and the failure counter is reset.)
- **No internet connection** (none of the configured calendars are reachable at the network level) → a localized **"no internet connection"** screen showing the date and the time the device will **automatically restart** (10 minutes later). Rebooting typically restores the Wi-Fi and updates resume. If the connection returns on its own before then, the reboot is cancelled.
- **Other update problem** (calendar server error, bad calendar data, rendering failure, …) → a localized **"update failed"** screen with the date. The plugin re-checks the connection: if it's actually down, it's treated as the no-internet case; otherwise it still shows the error and schedules a recovery reboot.
- **Nothing configured yet** (no calendar URL) → a localized **"no calendar configured"** screen, with **no** reboot (a reboot can't fix configuration).

**Reboot loop protection.** Consecutive auto-reboots are capped (default **3**). After that many failed updates in a row the plugin **stops rebooting** and just keeps showing the screen (now with a "persistent problem — please ask for help" note) so the device isn't stuck rebooting forever. The counter is remembered across reboots and is reset the moment a normal update succeeds.

**Always displays, even if the renderer breaks.** The status/error screen normally renders via the same HTML engine as the dashboard; if that itself fails, the plugin falls back to a plain built-in screen so it still shows the date and message.

**Other details:**

- The reboot is only triggered when the network is genuinely unreachable (timeout / connection refused / DNS). A reachable calendar *server* that returns an HTTP error (e.g. 404/500) shows the error screen but isn't, by itself, a reason to reboot — though a capped recovery reboot is still attempted in case it clears a transient glitch.
- With multiple calendars configured, the device is only considered offline when **none** of them can be reached.
- The displayed reboot time stays stable across refreshes during the same outage.
- The reboot uses the same `sudo reboot` mechanism as InkyPi's built-in Reboot button, so it relies on the passwordless-sudo setup that a standard InkyPi installation already configures. No extra setup is required.
- The weather block is best-effort: if the weather service is unreachable the dashboard still renders, just without weather.
- All screens are localized in the same languages as the rest of the plugin (English, German, Spanish, French) and follow the device's 12h/24h time format. The strings live in `constants.py` (`offlineTitle`, `offlineMessage`, `offlineClock`, `offlineReassure`, `errorTitle`, `errorMessage`, `rebootPrefix`, `configMessage`, `noRebootNote`).


## Installation

### Install

Install the plugin using the InkyPi CLI, providing the plugin ID and GitHub repository URL:

```bash
inkypi plugin install seniorDashboard_allDay https://github.com/RobinWts/InkyPi-Plugin-seniorDashboard_allDay
```

or install the [PluginManager](https://github.com/RobinWts/InkyPi-Plugin-PluginManager) first and use that to install via WebUI.


## Development-status

Feature complete and done for the moment. Probably will update in the future if needed.

## License

This project is licensed under the GNU public License.
