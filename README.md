# Shadow-economy monitor (Vilnius)

Surfaces businesses that look busy on Google but quiet in official records
(Sodra, Registrų centras). Output is a prioritised, explainable lead list for a tax analyst.

Categories: coffee shops, car repair / tyre shops, flower shops.

## Run

```bash
cp .env.example .env   # add GOOGLE_API_KEY
docker compose up
```

Open http://localhost:8501.

### Credentials

No credentials are committed. Official sources (Sodra, Registrų centras) are public.

- **With `GOOGLE_API_KEY` in `.env`**: fresh Google Places data is collected and saved
  as a new snapshot in `snapshots/google/`.
- **Without a key** (clean machine): the latest committed snapshot from a real run is
  replayed, and the rest of the pipeline runs normally. Each listing records its
  `source` (`live` or `snapshot:<file>`).

## Local development

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:PYTHONPATH = "src"
python -m shadow.pipeline
streamlit run app/streamlit_app.py
pytest
```

See [DECISIONS.md](DECISIONS.md) for design decisions and limitations.
