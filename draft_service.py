"""
Slack Email Draft Service
Monitors #inbox-assistant for email notifications and drafts responses in Laura Paris's style.
"""

import os
import sys
import time
import re
from typing import Optional

# Force unbuffered output for Railway logs
sys.stdout.reconfigure(line_buffering=True)
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import google.generativeai as genai

# Configuration
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "C0A525DKMR7")  # #inbox-assistant

# Check interval in seconds
CHECK_INTERVAL = 30

# Laura Paris Style Profile (condensed for prompt)
LAURA_STYLE_PROMPT = """
You are drafting email replies in the style of Laura Paris, Executive Director at Coalition for Hispanic Family Services.

## Laura's Writing Style:
- **Greetings**: Usually NO greeting (starts directly with content), or "Hi {name}," for individuals
- **Closings**: Just "Laura" (no "Best," or "Regards,")
- **Tone**: Professional-friendly, warm but efficient, direct
- **Sentence length**: Short, punchy sentences
- **Uses contractions**: I'm, don't, won't, aren't
- **Exclamation points**: Uses them for enthusiasm (sometimes double !!)
- **Abbreviations**: FYI, mon (for minutes)

## Common phrases Laura uses:
- "Sounds good." / "Sounds good to me."
- "OK!"
- "Thank you [Name]."
- "Please [verb]..." (direct requests)
- "Indeed, [confirmation]"
- "Glad to hear..."
- "Looking forward to..."

## What Laura NEVER does:
- Long formal greetings
- Excessive pleasantries
- Formal closings (Sincerely, Regards)
- Bullet points or numbered lists
- Long paragraphs
- Hedging language ("I was wondering if perhaps...")

## Reply Guidelines:
1. Start directly - Skip greeting unless it's formal
2. Keep it short - 1-3 sentences typical
3. Be direct - No hedging
4. Use "Please" for requests
5. Show warmth with exclamation points
6. Close with just "Laura"
"""


class SlackDraftService:
    def __init__(self):
        self.slack_client = WebClient(token=SLACK_BOT_TOKEN)
        genai.configure(api_key=GEMINI_API_KEY)
        self.model = genai.GenerativeModel('gemini-1.5-flash-latest')
        self.processed_messages = set()
        self.last_check_ts = str(time.time())

    def get_recent_messages(self) -> list:
        """Fetch recent messages from the inbox-assistant channel."""
        try:
            result = self.slack_client.conversations_history(
                channel=SLACK_CHANNEL_ID,
                oldest=self.last_check_ts,
                limit=20
            )
            messages = result.get("messages", [])
            print(f"[DEBUG] Fetched {len(messages)} messages since {self.last_check_ts}")
            return messages
        except SlackApiError as e:
            print(f"Error fetching messages: {e}")
            return []

    def parse_email_from_message(self, text: str) -> Optional[dict]:
        """Parse email details from Power Automate message format."""
        # Expected format: "[From] | Subject: [Subject] | Body Preview: [Body]"
        parts = text.split(" | ")
        if len(parts) < 3:
            return None

        email_data = {}

        # Extract From
        email_data["from"] = parts[0].strip()

        # Extract Subject
        for part in parts:
            if part.startswith("Subject:"):
                email_data["subject"] = part.replace("Subject:", "").strip()
            elif part.startswith("Body Preview:"):
                email_data["body"] = part.replace("Body Preview:", "").strip()

        if "subject" in email_data and "body" in email_data:
            return email_data
        return None

    def is_priority_sender(self, from_field: str) -> bool:
        """Check if the email is from Laura Paris (priority sender)."""
        priority_patterns = ["laura.paris", "lparis", "laura paris"]
        from_lower = from_field.lower()
        return any(pattern in from_lower for pattern in priority_patterns)

    def classify_email(self, email_data: dict) -> dict:
        """Classify email priority and type."""
        subject = email_data.get("subject", "").lower()
        body = email_data.get("body", "").lower()
        from_field = email_data.get("from", "")

        is_from_laura = self.is_priority_sender(from_field)

        # Urgency keywords
        urgent_keywords = ["urgent", "asap", "immediately", "emergency", "critical", "deadline today"]
        is_urgent = any(kw in subject or kw in body for kw in urgent_keywords)

        # Determine priority
        if is_from_laura:
            priority = "URGENT - FROM LAURA"
        elif is_urgent:
            priority = "URGENT"
        else:
            priority = "NORMAL"

        # Detect email type for better response drafting
        email_type = "general"
        if any(kw in subject + body for kw in ["meeting", "schedule", "calendar", "time"]):
            email_type = "scheduling"
        elif any(kw in subject + body for kw in ["question", "?"]):
            email_type = "question"
        elif any(kw in subject + body for kw in ["please", "request", "need", "can you"]):
            email_type = "request"
        elif any(kw in subject + body for kw in ["fyi", "update", "information", "attached"]):
            email_type = "fyi"
        elif any(kw in subject + body for kw in ["thank", "congrat", "great job", "well done"]):
            email_type = "acknowledgment"

        return {
            "priority": priority,
            "email_type": email_type,
            "is_from_laura": is_from_laura
        }

    def draft_response(self, email_data: dict, classification: dict) -> str:
        """Use Gemini to draft a response in Laura's style."""
        # Build the prompt
        prompt = f"""{LAURA_STYLE_PROMPT}

## Email to respond to:
**From:** {email_data.get('from', 'Unknown')}
**Subject:** {email_data.get('subject', 'No subject')}
**Body Preview:** {email_data.get('body', 'No body')}

## Classification:
- Priority: {classification['priority']}
- Type: {classification['email_type']}
- From Laura: {classification['is_from_laura']}

## Task:
Draft a SHORT reply to this email in Laura Paris's exact style.
- If it's FROM Laura, draft a reply TO Laura
- If it needs information you don't have, write "[Need info: specific question]"
- Keep it 1-4 sentences max
- End with just "Laura"

Draft reply:"""

        try:
            response = self.model.generate_content(prompt)
            draft = response.text.strip()
            return draft
        except Exception as e:
            print(f"Error generating draft: {e}")
            return f"[Error drafting response: {e}]"

    def post_draft_reply(self, channel: str, thread_ts: str, draft: str, classification: dict):
        """Post the drafted response as a thread reply."""
        priority_emoji = ":rotating_light:" if "URGENT" in classification["priority"] else ":memo:"

        message = f"""{priority_emoji} *Draft Response* ({classification['priority']})

{draft}

---
_This is an AI-generated draft in Laura's style. Review before sending._
React :white_check_mark: when approved or :x: to discard."""

        try:
            self.slack_client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=message
            )
            print(f"Posted draft reply to thread {thread_ts}")
        except SlackApiError as e:
            print(f"Error posting draft: {e}")

    def process_messages(self):
        """Main processing loop - check for new emails and draft responses."""
        messages = self.get_recent_messages()

        for msg in messages:
            msg_ts = msg.get("ts", "")
            msg_text = msg.get("text", "")

            # Skip if already processed
            if msg_ts in self.processed_messages:
                continue

            # Skip our own draft responses (not all bot messages - Power Automate uses bots too)
            if "Draft Response" in msg_text:
                self.processed_messages.add(msg_ts)
                continue

            # Try to parse as email notification
            email_data = self.parse_email_from_message(msg_text)
            if not email_data:
                self.processed_messages.add(msg_ts)
                continue

            print(f"Processing email: {email_data.get('subject', 'No subject')}")

            # Classify the email
            classification = self.classify_email(email_data)

            # Draft a response
            draft = self.draft_response(email_data, classification)

            # Post the draft as a thread reply
            self.post_draft_reply(SLACK_CHANNEL_ID, msg_ts, draft, classification)

            self.processed_messages.add(msg_ts)

        # Update timestamp for next check
        self.last_check_ts = str(time.time())

        # Cleanup old processed messages (keep last 500)
        if len(self.processed_messages) > 500:
            self.processed_messages = set(list(self.processed_messages)[-250:])

    def run(self):
        """Main run loop."""
        print(f"Starting Slack Draft Service")
        print(f"Monitoring channel: {SLACK_CHANNEL_ID}")
        print(f"Check interval: {CHECK_INTERVAL} seconds")
        print("-" * 50)

        # Send startup message
        try:
            self.slack_client.chat_postMessage(
                channel=SLACK_CHANNEL_ID,
                text=":robot_face: *AI Draft Service Started*\n\nI'll automatically draft responses in Laura's style when new emails arrive."
            )
        except SlackApiError as e:
            print(f"Error sending startup message: {e}")

        while True:
            try:
                self.process_messages()
            except Exception as e:
                print(f"Error in processing loop: {e}")

            time.sleep(CHECK_INTERVAL)


def main():
    # Validate configuration
    missing = []
    if not SLACK_BOT_TOKEN:
        missing.append("SLACK_BOT_TOKEN")
    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")

    if missing:
        print("Missing required environment variables:")
        for var in missing:
            print(f"  - {var}")
        print("\nPlease set these variables and try again.")
        return

    service = SlackDraftService()
    service.run()


if __name__ == "__main__":
    main()
