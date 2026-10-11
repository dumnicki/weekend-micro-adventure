Constraints:
- time_budget: {time_budget} ({budget_minutes} minutes total, including travel)
- transport: {transport}
- vibe: {vibe}

Now: {now_local}
Latest return time: {latest_return_time}
Weather: {weather_summary}

Candidates (JSON). Each one carries "activity_minutes_available", which is the
maximum number of minutes you may spend at that place:
{candidates_json}

Pick one and return the JSON card.

Set duration_minutes to the activity_minutes_available of the candidate you
picked, or to a smaller number. Do not use {budget_minutes}.