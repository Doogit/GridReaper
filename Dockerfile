# GridSignals (Streamlit) as a self-contained image for Azure App Service.
#
# Demo mode = LIVE INGEST AT BUILD. GridSignals ships only config seeds; the
# signal feed is empty until the pipeline runs against live public feeds. So the
# build runs the pipeline and bakes the resulting signals into the image.
#
# Tradeoff (accepted): the build is network-dependent and non-deterministic — it
# fetches from ~5 external feeds. Network ingests are non-fatal so one flaky
# feed can't fail the build, but we assert signals>0 at the end so a fully-empty
# result fails loudly instead of shipping a blank demo.
FROM python:3.12-slim

WORKDIR /app

# The pipeline + data layer are stdlib-only; requirements.txt is just Streamlit.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV GRIDSIGNALS_DB=/app/data/gridsignals.db

# load_seeds (schema + config) -> licensing (license facts; REQUIRED before
# plays, or play cards silently generate zero) -> ingest -> classify -> score ->
# plays. Ingests are wrapped so a single flaky feed degrades instead of failing
# the build; the final assertion guarantees the feed is not empty.
RUN python -m app.db.load_seeds \
 && python -m app.licensing \
 && (python -m app.ingest.edgar             || echo "WARN: edgar ingest failed, continuing") \
 && (python -m app.ingest.federal_register  || echo "WARN: federal_register ingest failed, continuing") \
 && (python -m app.ingest.presswire --source prnewswire    || echo "WARN: prnewswire ingest failed, continuing") \
 && (python -m app.ingest.presswire --source globenewswire || echo "WARN: globenewswire ingest failed, continuing") \
 && (python -m app.ingest.nerc_pages        || echo "WARN: nerc_pages ingest failed, continuing") \
 && (python -m app.ingest.cisa_kev          || echo "WARN: cisa_kev ingest failed, continuing") \
 && python -m app.classify.regulatory \
 && python -m app.classify.leadership \
 && python -m app.scoring \
 && python -m app.plays \
 && python -c "import sqlite3, os; n = sqlite3.connect(os.environ['GRIDSIGNALS_DB']).execute('select count(*) from signals').fetchone()[0]; assert n > 0, 'build pipeline produced no signals'; print(f'baked {n} signals')"

# App Service routes to the port named by the WEBSITES_PORT app setting; keep it
# in sync with the port Streamlit binds (see deploy/azure-deploy.ps1).
ENV PORT=8000
EXPOSE 8000

# Bind 0.0.0.0 so App Service can reach it (CLI flag overrides .streamlit/config).
# Easy Auth, when enabled, fronts the app, so this is not exposed to the open net.
CMD ["sh", "-c", "streamlit run app/ui/Home.py --server.address=0.0.0.0 --server.port=${PORT} --server.headless=true --browser.gatherUsageStats=false"]
