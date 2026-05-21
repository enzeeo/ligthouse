# ligthouse

Local-only storage dashboard.

## Run

```sh
python3 app.py
```

Then open `http://127.0.0.1:8765`.

## Test

```sh
python3 -m unittest
```

## Notes

- Backend uses Python standard library only.
- Frontend uses vanilla HTML, CSS, and JavaScript only.
- Dashboard is a sparse btop-style web UI for overview, review, file, folder, growth, and scanner log views.
- It is read-only: no delete or filesystem-mutating controls are exposed.
