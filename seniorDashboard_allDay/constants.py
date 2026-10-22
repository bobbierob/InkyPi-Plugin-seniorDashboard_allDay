FONT_SIZES = {
    "x-small": 0.7,
    "smaller": 0.8,
    "small": 0.9,
    "normal": 1,
    "large": 1.1,
    "larger": 1.2,
    "x-large": 1.3
}

# The plugin is English-only. LOCALE is passed to Intl.DateTimeFormat for the offline screen's date.
LOCALE = "en"

# Calendar marker colours, in the order new calendars get them. Pure panel colours, so they show
# cleanly on the e-paper display.
CALENDAR_COLORS = [
    ("#0000FF", "Blue"),
    ("#FF0000", "Red"),
    ("#000000", "Black"),
    ("#FFFF00", "Yellow"),
    ("#00FF00", "Green"),
]

# UI strings.
LABELS = {
    "allDayText": "All day",
    "nothingMoreToday": "Nothing more for today.",
    "today": "Today",
    "tomorrow": "Tomorrow",
    "offlineTitle": "No internet connection",
    "offlineMessage": "The device will restart automatically at",
    "offlineReassure": "Please wait a moment.",
    "errorTitle": "Update failed",
    "errorMessage": "There was a problem updating.",
    "rebootPrefix": "Automatic restart at",
    "configMessage": "No calendar configured.",
    "noRebootNote": "Persistent problem - please ask for help.",
    "noPrecipitation": "No precipitation",
    "iceWarning": "Risk of ice",
    "lastUpdated": "Last updated",
    "moreEvents": "+{n} more",
    "from": "from",
    "until": "until",
}

WEATHER_ICONS = {
    0: "☀️",   # klar
    1: "🌤️",  # meist klar
    2: "⛅",   # teilweise bewölkt
    3: "☁️",   # bedeckt
    45: "🌫️",  # Nebel
    48: "🌫️",
    51: "🌦️",  # Niesel
    53: "🌦️",
    55: "🌦️",
    56: "🧊",  # gefrierender Niesel
    57: "🧊",
    61: "🌧️",  # Regen
    63: "🌧️",
    65: "🌧️",
    66: "🧊",  # gefrierender Regen
    67: "🧊",
    71: "❄️",  # Schnee
    73: "❄️",
    75: "❄️",
    77: "🌨️",  # Schneegriesel
    80: "🌦️",  # Schauer
    81: "🌧️",
    82: "🌧️",
    85: "🌨️",  # Schneeschauer
    86: "🌨️",
    95: "⛈️",  # Gewitter
    96: "⛈️",
    99: "⛈️",
}
