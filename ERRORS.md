# ERRORS.md

## 2026-05-21 - Browser Helper Parallel Commands Exhausted Startup
- Context: During UI smoke testing, multiple `gstack browse` commands were launched in parallel after a fresh helper start.
- Failed: Parallel `snapshot`, `screenshot`, `console`, and `network` calls raced each other, produced `about:blank` output, and then failed with `No available port after 5 attempts in range 10000-60000`.
- Worked: HTTP smoke with `curl` verified the local app and API. Stop/retry the browser helper, then run browse commands sequentially or use one `chain` call after a confirmed `goto`.
- Check next time: Do not parallelize browser helper commands; run `browse stop`, `browse goto http://127.0.0.1:8765`, then `browse snapshot` / `browse screenshot` one at a time.
