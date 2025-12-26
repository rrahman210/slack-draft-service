"""
Slack Email Draft Service - @CHFSDraftBot
Drafts email responses in Laura Paris's style when @mentioned in Slack threads.
Tag @CHFSDraftBot in any email thread to get a draft, then refine with commands.
"""

import os
import sys
import time
import re
from typing import Optional
from collections import deque

# Force unbuffered output for Railway logs
sys.stdout.reconfigure(line_buffering=True)
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from google import genai

# Configuration
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "C0A525DKMR7")  # #inbox-assistant

# Check interval in seconds
CHECK_INTERVAL = 30

# Laura Paris Style Profile (learned from real emails - Dec 2025)
LAURA_STYLE_PROMPT = """
You are drafting email replies in the style of Laura Paris, Executive Director at Coalition for Hispanic Family Services.

## Laura's Writing Style (from real email analysis):
- **Greetings**: NO greeting - starts directly with content (confirmed from multiple emails)
- **Closings**: "Best," on its own line, then signature block:
  Laura Paris
  Executive Director
  (On mobile/quick replies: may skip closing entirely)
- **Tone**: Professional, direct, action-oriented
- **Sentence length**: Short, direct sentences
- **Uses contractions**: I'm, don't, won't, aren't
- **Soft suggestions**: Uses "Perhaps" to soften suggestions/alternatives

## Common phrases Laura uses:
- "Perhaps [alternative suggestion]" (for offering options)
- "Please [verb]..." (direct requests)
- "Thank you [Name]."
- "Sounds good."
- "OK!"
- "Looking forward to..."

## What Laura NEVER does:
- Start with greetings like "Hi," or "Hello,"
- Use formal closings like "Sincerely," or "Regards,"
- Write long paragraphs
- Hedge excessively
- Use bullet points or numbered lists in replies

## Reply Guidelines:
1. Start directly with content - NO greeting
2. Keep it short - 1-3 sentences typical
3. Be direct and action-oriented
4. Use "Please" for requests
5. Use "Perhaps" when suggesting alternatives
6. Close with:
   Best,

   Laura Paris
   Executive Director
   (or skip closing for very quick replies)
"""


class SlackDraftService:
    def __init__(self):
        self.slack_client = WebClient(token=SLACK_BOT_TOKEN)
        self.genai_client = genai.Client(api_key=GEMINI_API_KEY)
        self.model_name = 'gemini-2.5-flash'
        self.processed_messages = deque(maxlen=500)  # Auto-evicts oldest when full
        # Look back 1 hour at startup to catch recent threads
        self.last_check_ts = str(time.time() - 3600)
        self.bot_user_id = None  # Set in run() via auth_test()

        # Supported refinement commands
        self.commands = {
            "shorter": "Make the draft more concise - reduce to 1-2 sentences",
            "longer": "Expand the draft with more detail",
            "formal": "Make the tone more formal and professional",
            "casual": "Make the tone more casual and friendly",
            "rewrite": "Generate a completely new draft from scratch"
        }

    def get_recent_messages(self) -> list:
        """Fetch recent messages from the inbox-assistant channel."""
        try:
            # Always look back 6 hours to catch threads with new activity
            lookback_ts = str(time.time() - 21600)  # 6 hours
            result = self.slack_client.conversations_history(
                channel=SLACK_CHANNEL_ID,
                oldest=lookback_ts,
                limit=50
            )
            messages = result.get("messages", [])
            print(f"[DEBUG] Fetched {len(messages)} messages from last 6 hours")
            return messages
        except SlackApiError as e:
            print(f"Error fetching messages: {e}")
            return []

    def contains_bot_mention(self, text: str) -> bool:
        """Check if message mentions this bot."""
        if not self.bot_user_id:
            return False
        mention_pattern = f"<@{self.bot_user_id}>"
        return mention_pattern in text

    def parse_command(self, text: str) -> Optional[str]:
        """Extract command from message text (after the @mention).

        Uses word boundary matching to avoid false positives.
        """
        if not self.bot_user_id:
            return None

        mention_pattern = f"<@{self.bot_user_id}>"
        if mention_pattern not in text:
            return None

        # Get text after the mention
        text_after_mention = text.split(mention_pattern, 1)[1].strip().lower()

        if not text_after_mention:
            # Just @mention with no command = generate draft
            return "draft"

        # Extract first word only (removes punctuation)
        first_word = text_after_mention.split()[0].rstrip('.,!?;:')

        # Check if first word is a known command
        if first_word in self.commands:
            print(f"[CMD] Matched command: {first_word}")
            return first_word

        # Unknown text = treat as new draft request
        print(f"[CMD] Unknown command '{first_word}', treating as draft request")
        return "draft"

    def get_thread_messages(self, channel: str, thread_ts: str) -> list:
        """Get all messages in a thread."""
        try:
            result = self.slack_client.conversations_replies(
                channel=channel,
                ts=thread_ts
            )
            return result.get("messages", [])
        except SlackApiError as e:
            print(f"Error fetching thread: {e}")
            return []

    def get_last_draft_from_thread(self, thread_messages: list) -> Optional[str]:
        """Find the most recent draft in a thread (for refinement).

        Uses regex for more robust extraction.
        """
        for msg in reversed(thread_messages):
            text = msg.get("text", "")

            # Skip if no draft marker
            if "Draft Response" not in text:
                continue

            # Try regex extraction first (most reliable)
            # Pattern: *Draft Response* (optional text) \n\n CONTENT \n\n ---
            match = re.search(
                r'\*Draft Response\*[^\n]*\n\n(.*?)\n\n---',
                text,
                re.DOTALL
            )
            if match:
                draft = match.group(1).strip()
                if draft:
                    print(f"[EXTRACT] Found draft via regex ({len(draft)} chars)")
                    return draft

            # Fallback: line-by-line parsing
            lines = text.split("\n")
            draft_lines = []
            in_draft = False
            for line in lines:
                if "*Draft Response*" in line or "Draft Response" in line:
                    in_draft = True
                    continue
                if line.startswith("---") or "_Reply with @CHFSDraftBot" in line:
                    break
                if in_draft and line.strip():
                    draft_lines.append(line)

            if draft_lines:
                draft = "\n".join(draft_lines).strip()
                print(f"[EXTRACT] Found draft via fallback ({len(draft)} chars)")
                return draft

        print(f"[EXTRACT] No draft found in {len(thread_messages)} thread messages")
        return None

    def parse_email_from_message(self, text: str) -> Optional[dict]:
        """Parse email details from inbox-monitor message format.

        Expected format (first line): "[From] | Subject: [Subject] | Body Preview: [Body]"
        Handles cases where subject/body contains pipe characters.
        """
        # Only parse the first line (machine-readable format)
        first_line = text.split("\n")[0].strip()

        # Use markers to split more reliably (handles pipes in content)
        if " | Subject: " not in first_line or " | Body Preview: " not in first_line:
            print(f"[PARSE] Missing markers in: {first_line[:80]}...")
            return None

        try:
            # Split by Body Preview first (appears last)
            before_body, body_part = first_line.split(" | Body Preview: ", 1)

            # Then split the remaining part by Subject
            from_part, subject_part = before_body.split(" | Subject: ", 1)

            # Clean and validate
            from_clean = from_part.strip()
            subject_clean = subject_part.strip()
            body_clean = body_part.strip()

            if not from_clean or not subject_clean:
                print(f"[PARSE] Empty required field - from: '{from_clean}', subject: '{subject_clean}'")
                return None

            return {
                "from": from_clean,
                "subject": subject_clean,
                "body": body_clean if body_clean else "(No preview)"
            }
        except ValueError as e:
            print(f"[PARSE] Error splitting message: {e}")
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

    def draft_response(self, email_data: dict, classification: dict, retry_count: int = 0) -> str:
        """Use Gemini to draft a response in Laura's style.

        Includes retry logic with exponential backoff for rate limits.
        """
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
- End with the signature block

Draft reply:"""

        max_retries = 3
        base_delay = 2

        try:
            response = self.genai_client.models.generate_content(
                model=self.model_name,
                contents=prompt
            )
            draft = response.text.strip()
            return draft
        except Exception as e:
            error_msg = str(e)
            print(f"[GEMINI] Error generating draft: {error_msg}")

            # Check for rate limiting (429 errors)
            if "429" in error_msg or "quota" in error_msg.lower() or "rate" in error_msg.lower():
                if retry_count < max_retries:
                    wait_time = base_delay * (2 ** retry_count)  # 2s, 4s, 8s
                    print(f"[GEMINI] Rate limited, waiting {wait_time}s before retry {retry_count + 1}/{max_retries}")
                    time.sleep(wait_time)
                    return self.draft_response(email_data, classification, retry_count + 1)
                else:
                    print(f"[GEMINI] Max retries exceeded")
                    return "[Error: Rate limit exceeded - please try again in a few minutes]"

            return f"[Error: Could not generate draft - {type(e).__name__}]"

    def refine_draft(self, original_draft: str, command: str, email_data: dict, retry_count: int = 0) -> str:
        """Refine an existing draft based on a command.

        Includes retry logic with exponential backoff for rate limits.
        """
        command_instruction = self.commands.get(command, "Improve the draft")

        prompt = f"""{LAURA_STYLE_PROMPT}

## Original Email Context:
**From:** {email_data.get('from', 'Unknown')}
**Subject:** {email_data.get('subject', 'No subject')}
**Body Preview:** {email_data.get('body', 'No body')}

## Current Draft:
{original_draft}

## Refinement Request: {command.upper()}
{command_instruction}

## Task:
Rewrite the draft following the refinement request while maintaining Laura Paris's style.
Output ONLY the refined draft, nothing else.

Refined draft:"""

        max_retries = 3
        base_delay = 2

        try:
            response = self.genai_client.models.generate_content(
                model=self.model_name,
                contents=prompt
            )
            return response.text.strip()
        except Exception as e:
            error_msg = str(e)
            print(f"[GEMINI] Error refining draft: {error_msg}")

            # Check for rate limiting (429 errors)
            if "429" in error_msg or "quota" in error_msg.lower() or "rate" in error_msg.lower():
                if retry_count < max_retries:
                    wait_time = base_delay * (2 ** retry_count)
                    print(f"[GEMINI] Rate limited, waiting {wait_time}s before retry {retry_count + 1}/{max_retries}")
                    time.sleep(wait_time)
                    return self.refine_draft(original_draft, command, email_data, retry_count + 1)
                else:
                    print(f"[GEMINI] Max retries exceeded")
                    return "[Error: Rate limit exceeded - please try again in a few minutes]"

            return f"[Error: Could not refine draft - {type(e).__name__}]"

    def post_draft_reply(self, channel: str, thread_ts: str, draft: str, classification: dict, is_refinement: bool = False):
        """Post the drafted response as a thread reply."""
        priority_emoji = ":rotating_light:" if "URGENT" in classification["priority"] else ":memo:"
        refinement_note = " _(refined)_" if is_refinement else ""

        message = f"""{priority_emoji} *Draft Response*{refinement_note}

{draft}

---
_Reply with @CHFSDraftBot + command to refine:_
`shorter` · `longer` · `formal` · `casual` · `rewrite`"""

        try:
            self.slack_client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=message
            )
            print(f"Posted draft reply to thread {thread_ts}")
        except SlackApiError as e:
            print(f"Error posting draft: {e}")

    def process_mention(self, msg_ts: str, msg_text: str, target_thread: str):
        """Process a single mention - generate or refine a draft."""
        print(f"[MENTION] Bot mentioned in message: {msg_ts}")

        # Parse the command (draft, shorter, longer, etc.)
        command = self.parse_command(msg_text)
        print(f"[COMMAND] Detected command: {command}")

        # Get all messages in the thread to find context
        thread_messages = self.get_thread_messages(SLACK_CHANNEL_ID, target_thread)

        # Find the original email (first message in thread should be the email notification)
        email_data = None
        for thread_msg in thread_messages:
            email_data = self.parse_email_from_message(thread_msg.get("text", ""))
            if email_data:
                break

        if not email_data:
            # No email found in thread - let user know
            try:
                self.slack_client.chat_postMessage(
                    channel=SLACK_CHANNEL_ID,
                    thread_ts=target_thread,
                    text=":warning: I couldn't find an email in this thread. Tag me on an email notification thread to draft a response."
                )
            except SlackApiError as e:
                print(f"Error posting error message: {e}")
            return

        # Classify the email
        classification = self.classify_email(email_data)

        # Handle refinement vs new draft
        if command != "draft" and command in self.commands:
            # This is a refinement request - find the last draft
            last_draft = self.get_last_draft_from_thread(thread_messages)
            if last_draft:
                print(f"[REFINE] Refining draft with command: {command}")
                refined_draft = self.refine_draft(last_draft, command, email_data)
                self.post_draft_reply(SLACK_CHANNEL_ID, target_thread, refined_draft, classification, is_refinement=True)
            else:
                # No previous draft found, create new one
                print(f"[DRAFT] No previous draft found, creating new one")
                draft = self.draft_response(email_data, classification)
                self.post_draft_reply(SLACK_CHANNEL_ID, target_thread, draft, classification)
        else:
            # New draft request
            print(f"[DRAFT] Creating new draft for: {email_data.get('subject', 'No subject')}")
            draft = self.draft_response(email_data, classification)
            self.post_draft_reply(SLACK_CHANNEL_ID, target_thread, draft, classification)

    def process_messages(self):
        """Process messages - check channel AND thread replies for @mentions."""
        messages = self.get_recent_messages()

        for msg in messages:
            msg_ts = msg.get("ts", "")
            msg_text = msg.get("text", "")
            reply_count = msg.get("reply_count", 0)

            # Check if this top-level message has threads with potential mentions
            if reply_count > 0:
                # Scan the thread for any unprocessed mentions
                thread_messages = self.get_thread_messages(SLACK_CHANNEL_ID, msg_ts)
                for thread_msg in thread_messages:
                    thread_msg_ts = thread_msg.get("ts", "")
                    thread_msg_text = thread_msg.get("text", "")

                    # Skip if already processed
                    if thread_msg_ts in self.processed_messages:
                        continue

                    # Skip our own draft responses
                    if "Draft Response" in thread_msg_text:
                        self.processed_messages.append(thread_msg_ts)
                        continue

                    # Check for bot mention in thread reply
                    if self.contains_bot_mention(thread_msg_text):
                        self.process_mention(thread_msg_ts, thread_msg_text, msg_ts)
                        self.processed_messages.append(thread_msg_ts)

            # Also check top-level messages for mentions (in case someone mentions bot there)
            if msg_ts not in self.processed_messages:
                if "Draft Response" in msg_text:
                    self.processed_messages.append(msg_ts)
                elif self.contains_bot_mention(msg_text):
                    self.process_mention(msg_ts, msg_text, msg_ts)
                    self.processed_messages.append(msg_ts)
                else:
                    self.processed_messages.append(msg_ts)

        # Update timestamp for next check
        self.last_check_ts = str(time.time())
        # Note: processed_messages is a deque with maxlen=500, auto-evicts oldest

    def run(self):
        """Main run loop."""
        print(f"Starting Slack Draft Service - @CHFSDraftBot")
        print(f"Monitoring channel: {SLACK_CHANNEL_ID}")
        print(f"Check interval: {CHECK_INTERVAL} seconds")
        print("-" * 50)

        # Get bot's user ID for mention detection
        try:
            auth_response = self.slack_client.auth_test()
            self.bot_user_id = auth_response.get("user_id")
            bot_name = auth_response.get("user", "CHFSDraftBot")
            print(f"Bot authenticated: @{bot_name} (ID: {self.bot_user_id})")
        except SlackApiError as e:
            print(f"FATAL: Could not authenticate bot: {e}")
            return

        # Send startup message
        try:
            self.slack_client.chat_postMessage(
                channel=SLACK_CHANNEL_ID,
                text=f""":robot_face: *CHFSDraftBot Online*

Tag me in any email thread to get a draft response in Laura's style.

*Commands:*
• `@CHFSDraftBot` - Generate draft
• `@CHFSDraftBot shorter` - Make it more concise
• `@CHFSDraftBot longer` - Add more detail
• `@CHFSDraftBot formal` - More formal tone
• `@CHFSDraftBot casual` - More casual tone
• `@CHFSDraftBot rewrite` - Fresh draft"""
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
