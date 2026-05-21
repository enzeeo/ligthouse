# ERRORS.md

## 2026-05-21 - Browser Helper Parallel Commands Exhausted Startup
- Context: During UI smoke testing, multiple `gstack browse` commands were launched in parallel after a fresh helper start.
- Failed: Parallel `snapshot`, `screenshot`, `console`, and `network` calls raced each other, produced `about:blank` output, and then failed with `No available port after 5 attempts in range 10000-60000`.
- Failed again: A later sequential `chain` attempt also failed with `No available port after 5 attempts in range 10000-60000`, and `pgrep -fl "gstack|browse"` found no stuck helper process.
- Failed in finish pass: sequential `goto http://127.0.0.1:8765` reported 200, but the follow-up `snapshot -i` started a fresh helper state and returned `about:blank`.
- Worked: HTTP smoke with `curl` verified the local app and API. Unit/static tests and subagent reviews covered the rest.
- Check next time: Do not parallelize browser helper commands; run `browse stop`, `browse goto http://127.0.0.1:8765`, then `browse snapshot` / `browse screenshot` one at a time.
