from __future__ import annotations

PROMPT_VERSION = "v1"

SYSTEM_PROMPT = """
You are a strict webhook normalization engine.
Classify a vendor event payload and normalize it to canonical fields.
You must return only valid JSON and no additional text.
""".strip()

USER_PROMPT_TEMPLATE = """
Normalize this event payload.

Vendor context:
{vendor_hint}

Rules:
1) entity_type must be one of: SHIPMENT, INVOICE, UNCLASSIFIED.
2) SHIPMENT canonical_status must be one of: PICKED_UP, IN_TRANSIT, OUT_FOR_DELIVERY, DELIVERED.
3) INVOICE canonical_status must be one of: ISSUED, PAID, VOIDED, REFUNDED.
4) UNCLASSIFIED canonical_status must be UNKNOWN.
5) event_time must be RFC3339 ISO-8601 with timezone.
6) confidence_score must be float in [0.0, 1.0].
7) infer entity_id from best available identifier.
8) use these examples:
   - "Loaded onboard and sailed" => SHIPMENT / IN_TRANSIT
   - "Cargo released to consignee" => SHIPMENT / DELIVERED
   - "freight invoice raised" => INVOICE / ISSUED
   - "settled in full" => INVOICE / PAID

Return JSON schema:
{{
  "entity_type": "SHIPMENT|INVOICE|UNCLASSIFIED",
  "entity_id": "string",
  "canonical_status": "string",
  "event_time": "ISO-8601 string",
  "confidence_score": 0.0,
  "normalized_payload": {{
      "summary": "short text",
      "signals": ["signal1", "signal2"]
  }}
}}

Payload:
{payload_json}
""".strip()
