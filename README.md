# ligthouse

Local-only storage dashboard.

## Run

```sh
python3 -m pip install -e .
lighthouse
```

Use `lighthouse web` to run the browser dashboard at `http://127.0.0.1:8765`.

## Test

```sh
python3 -m unittest
```

## Notes

- Backend uses Python standard library only.
- Frontend uses vanilla HTML, CSS, and JavaScript only.
- `lighthouse` opens a sparse btop-style terminal UI for overview, review, file, folder, growth, and scanner log views.
- `lighthouse web` preserves the browser dashboard.
- Cached compatible snapshots render first; stale or missing cache starts a background scan.
- It is read-only: no delete or filesystem-mutating controls are exposed.
