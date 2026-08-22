FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

COPY google-all-things-agentic-submission/cloud/requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --requirement /app/requirements.txt

COPY engineering-loop/antigravity_gemini_provider.py /app/
COPY engineering-loop/antigravity_google_genai_backend.py /app/
COPY engineering-loop/antigravity_isolated_child.py /app/
COPY engineering-loop/antigravity_isolated_transport.py /app/
COPY engineering-loop/antigravity_isolated_transport_schema.py /app/
COPY engineering-loop/antigravity_parent_supervision.py /app/
COPY engineering-loop/antigravity_transport_protocol.py /app/
COPY engineering-loop/google_agentic_demo.py /app/
COPY engineering-loop/m031_llm_execution_adapter.py /app/
COPY engineering-loop/mission_execution_contracts.py /app/
COPY engineering-loop/mission_executor.py /app/
COPY engineering-loop/mission_generator_candidates.py /app/
COPY engineering-loop/mission_generator_llm_producer.py /app/
COPY engineering-loop/mission_proposal_staging.py /app/
COPY engineering-loop/ollama_qwen_provider.py /app/
COPY engineering-loop/provider_capability_registry.py /app/
COPY engineering-loop/consensus_gate.py /app/
COPY engineering-loop/gemma_severity_classifier.py /app/
COPY engineering-loop/quarantine_report_generator.py /app/
COPY engineering-loop/lyria_alert_sound.py /app/
COPY engineering-loop/red_team_attacker.py /app/
COPY engineering-loop/red_team_incident.py /app/
COPY engineering-loop/red_team_session.py /app/
COPY google-all-things-agentic-submission/cloud/google_agentic_cloud_service.py /app/
RUN python -m venv /app/.antigravity_isolated_venv \
 && /app/.antigravity_isolated_venv/bin/pip install --no-cache-dir google-genai==2.18.1
RUN useradd --create-home --uid 10001 demo && chown -R demo:demo /app
USER 10001

EXPOSE 8080
CMD ["python", "/app/google_agentic_cloud_service.py"]

