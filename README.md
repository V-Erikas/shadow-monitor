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
