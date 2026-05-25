from __future__ import annotations

import json
import re
import sys

import anthropic

from config import ANTHROPIC_API_KEY, CLASSIFIER_BATCH_SIZE
from gmail_client import GmailClient

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

_SYSTEM_PROMPT = """You are an email classifier that identifies junk email for inbox cleanup.

Classify each email as junk or keep:
- JUNK: newsletters, marketing, promotions, sale alerts, social media notifications, automated digests, subscription confirmations, surveys, spam
- KEEP: personal messages, important receipts/invoices, security alerts, account verifications still needed, work emails, anything requiring a response

Be aggressive — promotional and automated emails should almost always be junk.

Categories to use (pick the closest one):
  newsletters | promotions | social | automated | spam | other"""

# Claude Sonnet 4.6 pricing (per 1M tokens)
_INPUT_COST_PER_1M = 3.0
_OUTPUT_COST_PER_1M = 15.0


def classify_emails(emails: list[dict]) -> tuple[list[dict], dict]:
    """Annotates each email with is_junk (bool), category (str), and reason (str).

    Returns (annotated_emails, token_usage) where token_usage has keys:
    input_tokens, output_tokens, cost_usd.

    Scope: Only processes emails from Primary inbox (excludes Gmail's Promotions & Spam categories).
    Protected emails (with custom labels or replied-to) are marked as keep automatically.
    Batches emails and logs token usage + cost per batch.
    """
    if not emails:
        return [], {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    gmail_client = GmailClient()
    protected = []
    unprotected = []

    for email in emails:
        if gmail_client.is_email_protected(email):
            protected.append(email)
        else:
            unprotected.append(email)

    total_input_tokens = 0
    total_output_tokens = 0

    # Classify unprotected emails
    results = []
    num_batches = (len(unprotected) + CLASSIFIER_BATCH_SIZE - 1) // CLASSIFIER_BATCH_SIZE

    for batch_idx in range(0, len(unprotected), CLASSIFIER_BATCH_SIZE):
        batch_num = (batch_idx // CLASSIFIER_BATCH_SIZE) + 1
        batch = unprotected[batch_idx : batch_idx + CLASSIFIER_BATCH_SIZE]

        # Show progress
        start_email = batch_idx + 1
        end_email = min(batch_idx + CLASSIFIER_BATCH_SIZE, len(unprotected))
        print(f"Classifying emails {start_email}-{end_email} of {len(unprotected)}...", file=sys.stderr)

        classified, input_tokens, output_tokens = _classify_batch(batch)
        results.extend(classified)

        total_input_tokens += input_tokens
        total_output_tokens += output_tokens

        # Calculate and log cost for this batch
        input_cost = (input_tokens / 1_000_000) * _INPUT_COST_PER_1M
        output_cost = (output_tokens / 1_000_000) * _OUTPUT_COST_PER_1M
        batch_cost = input_cost + output_cost

        print(
            f"  Batch {batch_num}/{num_batches}: {input_tokens} input + {output_tokens} output tokens "
            f"(${batch_cost:.4f})",
            file=sys.stderr,
        )

    # Mark protected emails as keep
    for email in protected:
        results.append({
            **email,
            "is_junk": False,
            "category": "other",
            "reason": "Protected (labeled or replied-to)",
        })

    # Log total usage
    total_cost = ((total_input_tokens / 1_000_000) * _INPUT_COST_PER_1M) + (
        (total_output_tokens / 1_000_000) * _OUTPUT_COST_PER_1M
    )
    print(
        f"\nTotal classification: {total_input_tokens} input + {total_output_tokens} output tokens "
        f"(${total_cost:.4f})",
        file=sys.stderr,
    )

    token_usage = {
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "cost_usd": round(total_cost, 6),
    }
    return results, token_usage


def _classify_batch(emails: list[dict]) -> tuple[list[dict], int, int]:
    """Classifies a batch of emails. Returns (classifications, input_tokens, output_tokens)."""
    email_text = "\n\n".join(
        f"ID: {e['id']}\nFrom: {e['sender']}\nSubject: {e['subject']}\nDate: {e['date']}\nSnippet: {e['snippet']}"
        for e in emails
    )

    prompt = (
        f"Classify these {len(emails)} emails. "
        "Return a JSON array only — no other text, no markdown fences.\n\n"
        "Each item must have:\n"
        '- "id": the email ID string\n'
        '- "is_junk": true or false\n'
        '- "category": one of newsletters|promotions|social|automated|spam|other\n'
        '- "reason": max 8 words explaining why\n\n'
        f"Emails:\n\n{email_text}"
    )

    try:
        response = _client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": prompt}],
        )
        classifications = _parse_json(response.content[0].text)
        classification_map = {c["id"]: c for c in classifications}

        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
    except Exception as exc:
        print(f"Warning: classifier batch failed ({exc}), marking batch as keep.", file=sys.stderr)
        classification_map = {}
        input_tokens = 0
        output_tokens = 0

    result = [
        {
            **email,
            "is_junk": classification_map.get(email["id"], {}).get("is_junk", False),
            "category": classification_map.get(email["id"], {}).get("category", "other"),
            "reason": classification_map.get(email["id"], {}).get("reason", ""),
        }
        for email in emails
    ]

    return result, input_tokens, output_tokens


def _parse_json(text: str) -> list:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)
