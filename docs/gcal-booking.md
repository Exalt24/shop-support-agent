# Google Calendar booking

**Register: built and tested against a stand-in, and run once for real on a throwaway calendar.** The stand-in (`agent/gcal_fake.py`) is written from the pages saved in `docs/vendor-contract/gcal-*.txt`. On 2026-11-14 the same code ran once against Google Calendar itself, on a temporary secondary calendar in a personal Google account that was deleted straight after (`docs/test-runs/2026-11-14-gcal-real-run.txt`): free/busy read, one event inserted with the derived id, the booked time then reported busy by Google. No guest was invited and the primary calendar was never touched. It is not wired into `serve.py`: attach it with `SupportAgent(..., booking=BookingChat(BookingDesk(GoogleCalendar(calendar_id, token_provider))))`. The OAuth exchange is the caller's: the saved pages do not print the refresh request, so the code takes a token provider and does not build that request by hand.

## The flow

1. A visitor writes something about an appointment. The agent's escalation floor runs first (a legal threat still goes to a person), then `BookingChat` offers the next three free times, at most one a day.
2. The visitor replies with a number or "second". The agent asks for a name.
3. `BookingDesk.book` checks the time against the business rules, reads free/busy again right before the write, inserts one event and confirms. A time that was taken in between makes a new offer, not an error.

Every reply is written in code from the calendar's own times, so no model wording and no grounding guard is involved, and a time that was never offered can never be said.

## Rules, each with the line it comes from

| Rule | Source page | Pinned by |
|---|---|---|
| Free/busy is a POST to `/freeBusy` with `timeMin`, `timeMax`, `timeZone` and `items[]` | `gcal-freebusy-query.txt` | `test_freebusy_sends_the_documented_url_and_body` |
| A calendar answered with `errors[]` was not read, and is never treated as free (the page shows these inside a successful response) | `gcal-freebusy-query.txt` | `test_a_calendar_that_comes_back_with_errors_is_not_read_as_free` |
| `busy` is start-inclusive and end-exclusive, so a slot that touches a meeting is free | `gcal-freebusy-query.txt` | `test_a_slot_that_starts_when_a_busy_block_ends_is_free` |
| An event id is lowercase a-v and 0-9, 5 to 1024 characters; collisions "cannot be guaranteed" to be detected, so the id is derived from slot and visitor, a 409 is read back, and an unknown outcome is looked up before a re-send | `gcal-events-insert.txt` | `test_a_lost_answer_is_looked_up_before_the_event_is_sent_again`, `test_a_409_for_the_same_start_is_a_success_and_for_another_start_is_a_collision` |
| `sendUpdates` is `all`, `externalOnly` or `none`; a visitor with no valid email gets `none` and no guest | `gcal-events-insert.txt` | `test_send_updates_must_be_a_documented_value`, `test_without_a_valid_email_there_is_no_guest_and_no_invitation` |
| A 401 gets one new token from the provider; 403 `rateLimitExceeded` and `userRateLimitExceeded`, 429 and 5xx back off exponentially; 400, 404 and other 403 are not retried | `gcal-handle-api-errors.txt` | `test_a_second_401_is_final_and_the_token_is_refreshed_only_once`, `test_limits_and_outages_back_off_and_retry`, `test_permanent_errors_are_not_retried`, `test_backoff_doubles_and_is_capped` |

## The business rules

`Rules` holds the time zone, the open days, the opening and closing hour, the slot length, the lead time, the horizon and the number of offers. A chosen time is bookable only if it is one of the business's own slots and far enough ahead (`rules_ok`), a name is letters only (a phone number or an email typed as a name is refused), and the audit steps hold counts and kinds, never the name or the email.

## Proof

```
./.venv/Scripts/python.exe -m pytest tests/test_gcal_booking.py -q      # 124 tests, 40 seeded random worlds
./.venv/Scripts/python.exe tests/mutation_check.py GCal                  # 54 mutants, each guard removed on purpose, all caught
```

The random worlds drop connections, rate limit, fail with a 500 and lose answers after the event was created, with collision detection on and off, outside meetings already on the calendar and three visitors talking at once. They check that the same visitor never books the same time twice, no event overlaps an outside block, every event is one of the business's slots, and no reply names a time that was not offered.

## Limits

One process: the conversation state is in memory and forgets a visitor on restart. Free/busy then insert is not atomic, so two visitors choosing the same time in the same second can both succeed (Google does not stop overlapping events); the second read right before the write narrows that window, it does not close it. The stand-in knows no organizers, permissions or quotas, and the real run was one booking on one calendar, so real-world quota behaviour is untested. Time zone names come from the system's time zone database.
