#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib import error, request


@dataclass(frozen=True)
class DispatchResult:
    index: int
    status_code: int
    response_body: str


SHIPMENT_STATUSES = [
    "Loaded onboard and sailed",
    "Container picked up from shipper",
    "Out for final delivery",
    "Cargo released to consignee",
]

INVOICE_STATUSES = [
    "freight invoice raised",
    "settled in full",
    "invoice voided due to correction",
    "refund completed",
]


def iso_time(offset_minutes: int) -> str:
    dt = datetime.now(UTC) + timedelta(minutes=offset_minutes)
    return dt.isoformat()


def build_payload(i: int) -> dict[str, object]:
    kind = i % 4
    suffix = f"{i:03d}-{uuid.uuid4().hex[:8]}"

    if kind == 0:
        return {
            "vendor": "Maersk",
            "event_id": f"MRSK-EVT-{suffix}",
            "container_no": f"MAEU24{random.randint(100000, 999999)}",
            "event_description": random.choice(SHIPMENT_STATUSES),
            "event_time": iso_time(i),
            "location": random.choice(["SGSIN", "NLRTM", "USLAX", "AEJEA"]),
        }
    if kind == 1:
        return {
            "vendor": "Ocean Network Express",
            "externalEventId": f"ONE-{suffix}",
            "shipment_reference": f"ONEY{random.randint(1000000, 9999999)}",
            "status_text": random.choice(SHIPMENT_STATUSES),
            "occurred_at": iso_time(i + 1),
            "port": random.choice(["JPYOK", "CNSHA", "USSAV", "NLRTM"]),
        }
    if kind == 2:
        invoice_number = f"GFP-INV-{random.randint(1000000, 9999999)}"
        return {
            "vendor": "GlobalFreightPay",
            "id": f"{invoice_number}-EVT-{suffix}",
            "invoice_number": invoice_number,
            "message": random.choice(INVOICE_STATUSES),
            "timestamp": iso_time(i + 2),
            "amount": f"{random.uniform(150.0, 8500.0):.2f}",
            "currency": "USD",
        }
    return {
        "vendor": "MarineAdvisoryFeed",
        "id": f"MAR-ALERT-{suffix}",
        "title": random.choice(
            [
                "Port congestion warning",
                "Weather advisory update",
                "Berth capacity alert",
                "Navigation caution notice",
            ]
        ),
        "body": "Advisory bulletin for regional marine traffic.",
        "published_at": iso_time(i + 3),
        "severity": random.choice(["low", "medium", "high"]),
    }


def post_payload(
    endpoint: str, payload: dict[str, object], index: int, timeout: int
) -> DispatchResult:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Vendor": str(payload.get("vendor", "unknown")),
            # The service keys idempotency on this header when it is present,
            # so a rerun of this script with the same ids is a no-op rather
            # than a second copy of every event.
            "Idempotency-Key": str(
                payload.get("event_id") or payload.get("externalEventId") or payload.get("id")
            ),
            "X-Correlation-ID": str(uuid.uuid4()),
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            response_body = resp.read().decode("utf-8")
            return DispatchResult(index=index, status_code=resp.status, response_body=response_body)
    except error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        return DispatchResult(index=index, status_code=exc.code, response_body=response_body)
    except Exception as exc:
        return DispatchResult(index=index, status_code=0, response_body=f"request_error: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send synthetic webhook payloads to the ingestion endpoint."
    )
    parser.add_argument(
        "--endpoint",
        # The compose file publishes the web container on 8100.
        default="http://localhost:8100/api/webhooks/",
        help="Webhook endpoint URL.",
    )
    parser.add_argument("--count", type=int, default=50, help="Number of requests to send.")
    parser.add_argument("--timeout", type=int, default=10, help="Per-request timeout in seconds.")
    args = parser.parse_args()

    accepted = 0
    failed = 0

    print(f"Dispatching {args.count} webhooks to {args.endpoint}")
    for i in range(1, args.count + 1):
        payload = build_payload(i)
        result = post_payload(args.endpoint, payload, i, args.timeout)
        if result.status_code == 202:
            accepted += 1
        else:
            failed += 1
        print(f"[{i:03d}] status={result.status_code} body={result.response_body}")

    print("\nSummary")
    print(f"Accepted (202): {accepted}")
    print(f"Failed/other:   {failed}")


if __name__ == "__main__":
    main()
