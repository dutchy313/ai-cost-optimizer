"""A deliberately wasteful support bot, used to test the cost agent.

Every function here contains at least one anti-pattern from the skill.
Do not copy this code.
"""
import time
from datetime import datetime

import anthropic

client = anthropic.Anthropic()

POLICY_DOC = open("policy.md").read()  # roughly 6,000 tokens of static policy text


def answer_ticket(user_name: str, question: str) -> str:
    # Timestamp and user name in the system prompt break the cache prefix,
    # and there is no cache_control and no max_tokens.
    response = client.messages.create(
        model="claude-opus-5-5",
        system=f"Today is {datetime.now()}. You are helping {user_name}.\n\n{POLICY_DOC}",
        messages=[{"role": "user", "content": question}],
    )
    return response.content[0].text


def tag_ticket(text: str) -> str:
    # A top-tier model for a one-word label.
    for attempt in range(5):
        try:
            r = client.messages.create(
                model="claude-opus-5-5",
                max_tokens=16000,
                messages=[{"role": "user", "content": f"Label as sales or support: {text}"}],
            )
            return r.content[0].text
        except anthropic.RateLimitError:
            time.sleep(2)  # fixed sleep, no jitter, ignores Retry-After
    raise RuntimeError("gave up")


def invoice_total(lines: list[str]) -> str:
    # Asking the model to do arithmetic.
    r = client.messages.create(
        model="claude-sonnet-5-5",
        max_tokens=500,
        messages=[{"role": "user", "content": "Calculate the total with 20% VAT:\n" + "\n".join(lines)}],
    )
    return r.content[0].text


def nightly_report(tickets: list[str]) -> list[str]:
    # Offline work sent through the live API one call at a time.
    out = []
    for t in tickets:
        r = client.messages.create(
            model="claude-opus-5-5",
            max_tokens=2000,
            messages=[{"role": "user", "content": f"Summarise: {t}"}],
        )
        out.append(r.content[0].text)
    return out
