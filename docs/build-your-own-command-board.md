# Build Your Own Command Board

This is a guide for an AI assistant (ChatGPT, Gemini, Claude, Copilot, or any capable agent) to follow while helping a non-engineer business owner build a personal status board across all of their business lines. If you are the assistant, read this whole document first, then run the discovery questionnaire in section 3 before you build anything. If you are the business owner, hand this file to your assistant and say "help me build this."

The system described here was built by one person for their own use across several small businesses. It is not a product, it is a pattern. Adjust the tool choices to whatever the person already uses, and keep every step verifiable.

## 1. What you get, and what this is not

What you get: twice a day, a single page that lists every business line you run, and under each line, the things that need your attention, the things you are waiting on someone else for, the things quietly moving forward, and the things that are parked. Every item has two or three sentences of plain summary and a link back to its original source, an email, a chat message, a document.

The reason to trust it is not that the summaries are clever. It is that nothing on the board is invented. Every line is traceable to a real message or document, and the summarizer step is instructed, explicitly, never to state a fact it cannot point to in its source material.

What this is not: it is not a task manager. It does not create to-dos or nag you about deadlines. It does not replace your email or your calendar; it reads them and tells you what is going on. If you want to act on something, you go do it in the original tool. This system never posts, sends, or replies to anything on your behalf. It is read-only, always, and it is not real-time. It runs twice a day on a schedule. If something is on fire right now, this will not tell you until the next run.

## 2. The five parts

**Sources.** The places your business signal actually lives: email, calendar, chat tools (Slack, Teams, WhatsApp), task or CRM tools, any system that carries real status. Do not connect everything on day one. Start with the two or three sources that carry the most weight.

**Collector.** A small script, not an AI, that runs first and gathers raw context into plain files: recent activity, your last board, what is currently open across your connected tools. It does no summarizing and no judgment, only fetch and save. Keeping this step non-AI matters, it is cheap, fast, deterministic, and easy to debug.

**Summarizer.** An AI step that reads everything the collector gathered, plus whatever it can reach live, and produces one structured board file. Each item has a business line, a state, a short synopsis, and a source link. Its only job is synthesis; it never sends anything and never invents a fact it cannot cite.

**Delivery.** A step that makes the structured board visible on your phone or laptop without opening a terminal or code editor: a page you host, a document in your office suite, or a note in a tool you already check daily.

**Feedback loop.** A way to mark something handled so it stops showing up, and for the next run to know you marked it. Without this, the board fills with stale items and you stop trusting it.

## 3. Discovery questionnaire

Run this with the person before writing any code or configuring any schedule. Write the answers down; they become your build spec.

1. What are your business lines? List each by name, with your role and who else, if anyone, is the primary decision-maker.
2. For each line, where does the real signal arrive? Email address or domain, specific people's names, a Slack or Teams workspace, a shared drive, a CRM.
3. Which of those sources already have a connector or app integration (through an AI assistant, a Zapier-style tool, a first-party app), and which need a new one built?
4. Which computer will run this twice a day? Does it stay on and connected at those times? If more than one computer is involved, do any need to feed into the main one?
5. What schedule fits the person's day? Twice a day, once in the morning and once in the late afternoon, is a reasonable default.
6. What must never be posted, sent, or exposed? Client data, health information, account numbers, anything under an NDA. This becomes a hard rule for the summarizer and the delivery step.
7. What tool ecosystem does the person live in? This determines concrete choices below: Claude Code vs. another agent CLI, Google Workspace vs. Microsoft 365, macOS vs. Windows vs. Linux.
8. Is there a UI they already check daily where a fallback board could live (a Notion page, a Google Doc, a private webpage) if the primary delivery method is unavailable?

## 4. Step-by-step build order

Each step has a verify check. Do not move to the next step until the check passes.

**Step 1: Pick the host machine and the schedule.**
Choose the machine that is on and reachable at your chosen run times. On macOS, scheduling is done with launchd (a background job the operating system runs for you). On Windows, use Task Scheduler. On Linux, use cron. Set up one job now that just prints the date and time to a file, on the schedule you chose.
Verify: check the output file the next morning and the next evening. If it has two new timestamps at roughly the right times, scheduling works.

**Step 2: Build the collector script.**
Write a plain script (Python is a good default; anything the person's assistant is comfortable with works) that, with no AI involved, gathers local context: recent activity logs, notes files, the previous board if one exists, and the output of any command-line tools you already have for checking open items (for example, a GitHub CLI command listing open pull requests, if code repos are part of the business). It writes these to a folder of plain text or JSON files.
Verify: run it by hand once. Open the files it wrote and confirm they contain real, current information, not placeholders.

**Step 3: Wire in the live connectors.**
Connect email, calendar, and your primary chat tool through whatever your AI assistant supports natively (Claude Code supports Gmail, Google Calendar, Google Drive, and Slack through its connector settings; other assistants have their own "connected apps" panel). For any source the built-in connector cannot reach, a second chat workspace, Microsoft Teams, a CRM with no first-party connector, build a small read-only "watcher" script against that service's own API, scoped narrowly, writing its findings to a file the summarizer reads alongside the collector's output.
Verify: for each connector and watcher, ask the assistant to read one real, current item from it and quote it back. Confirm the quote is accurate.

**Step 4: Write the summarizer prompt and generate one board.**
Give the assistant the prompt skeleton in section 6, the schema in section 5, and the files from steps 2 and 3. Ask it to produce one board.json.
Verify: open the file. For every single item, check that the synopsis matches something real in the source files, and that the source_url actually points to that item. Reject and redo if you find one invented fact; that is the whole point of this system.

**Step 5: Decide on delivery.**
If you use Claude Code and claude.ai, the natural choice is a claude.ai Artifact page: an interactive HTML page that can, at the moment you open it, call your own Google Drive (or equivalent) connector to fetch the latest board and render it, with no server of your own required. Pin it in your sidebar.
If you are not on that ecosystem, or you want something that works from any browser without an AI session, build a small static webpage on a private GitHub Pages site, or simply drop the board into a Google Doc, a Notion page, or a OneNote page you already check daily. A plain document is a legitimate fallback UI, not a downgrade, if it is the thing you will actually open twice a day.
Verify: open the delivery surface on your phone, cold. Confirm it shows the board you generated in step 4.

**Step 6: Automate the whole run.**
Combine steps 2 through 5 into one script or one scheduled agent run, triggered by the job you set up in step 1, so it runs unattended twice a day.
Verify: let it run once, unattended, at the next scheduled time. Check that a new board appeared on your delivery surface without you doing anything.

**Step 7: Add the feedback loop.**
Add a simple mechanism, a checkbox in your document, a toggle on your webpage, or a small separate file, for marking an item handled. Update the summarizer prompt so the next run reads that file and drops or re-states those items instead of repeating them cold.
Verify: mark one item handled, run the whole pipeline again by hand, and confirm that item is gone or clearly marked as resolved on the new board.

**Step 8 (optional): Add a second machine as a satellite.**
If the person works from two computers, set up a small job on the second machine that gathers its own local context (recent work, notes, anything not yet on the main machine) and pushes it somewhere the main machine's collector can read it before it runs, for example a private git repository both machines share.
Verify: do something notable on the second machine, wait for its scheduled push, then run the main pipeline and confirm that item shows up on the board.

## 5. The board.json schema

```json
{
  "schema": 1,
  "generated_at": "2026-09-11T07:00:00-04:00",
  "lines": [
    {
      "id": "example-line",
      "name": "Example Business Line",
      "headline": "One-sentence state of this line right now",
      "items": [
        {
          "id": "item-001",
          "state": "needs_you",
          "title": "Short title of the item",
          "synopsis": "Two to three plain sentences describing what is happening, grounded in the source material. No invented numbers or facts.",
          "source_url": "https://link.back.to/the-original-email-message-or-document",
          "sessions": [],
          "updated": "2026-09-11T06:45:00-04:00",
          "due": null,
          "owner": "You"
        }
      ]
    }
  ]
}
```

`state` is one of four values: `needs_you` (requires your action or decision), `waiting` (you are blocked on someone else), `moving` (progressing without your input right now), or `parked` (deliberately on hold). `sessions` is an optional list of any related work-in-progress identifiers you track elsewhere (for example, names of open coding sessions, ticket numbers, or document names); leave it empty if you do not track that. `due` is optional and can be null. `owner` defaults to you but can name someone else if the item is being tracked on their behalf.

## 6. The summarizer prompt skeleton

Use this shape when instructing the AI step that writes board.json. Fill in the business lines and sources from your discovery questionnaire.

```
You are producing a status board from the context files provided. Follow these rules exactly.

1. Use only the business lines listed: [list them].
2. For each line, read the provided context (recent activity, memory notes,
   inbox search results, calendar events, chat messages, watcher outputs,
   the previous board) and identify concrete items: things needing a decision,
   things being waited on, things quietly progressing, things on hold.
3. Every item's synopsis must be traceable to something in the provided
   context. If you cannot find a source for a claim, do not include it.
4. If a source you would normally check was unavailable this run (a
   connector failed, a watcher had no new output), do not guess. Write the
   line's headline or the specific item as "not checked (reason)" instead
   of silently omitting it or inventing a status.
5. Never invent counts, dollar amounts, dates, or names that do not appear
   in the source material.
6. You do not have permission to send, post, reply to, or otherwise act on
   anything you read. Your only output is the board.json file.
7. Carry forward any item marked "handled" in the feedback file by dropping
   it, unless new source material shows it is not actually resolved, in
   which case re-state it and note why.
8. Output valid JSON matching the schema exactly. No extra commentary.
```

## 7. Security rules

Every connector and watcher gets the narrowest scope that does the job: read-only where the API supports it, limited to the specific channels, folders, or labels that carry business signal, not blanket access to everything.

Store API tokens outside any code repository, readable only by your own user account (on macOS or Linux, a file with owner-read-only permissions; on Windows, the built-in Credential Manager). Never commit a token to git, paste one into a chat with the AI, or let the summarizer's prompt or output contain one.

The AI performing summarization should never see raw credentials. It reads the output of the watcher and collector scripts, which already did the authenticated fetching; it does not need direct API keys.

Nothing in this system posts, sends, replies, schedules, or deletes anything automatically. An action step later, an auto-reply, an auto-scheduled meeting, is a separate, much more carefully reviewed addition, not a natural extension of the board.

**Entity separation.** Treat a card from one business line appearing in front of someone from another as the same class of defect as an invented fact. The board is built for one pair of eyes: yours. Anything that leaves your device comes from a scoped export of a single line (the page's per-line "Share" view, or `board_export.py --line <id>`), never a whole-board screenshot or the full markdown. Run health, handled ids, and session names never travel with an export, because each of those can name another line's people, deals, or projects. If a client or partner is evaluating how you govern delivery, the tool that demonstrates discipline has to practice it on itself.

## 8. Operating it

To mark something handled, use whatever feedback mechanism you built in step 7: check a box, flip a toggle, or edit the feedback file directly. The next scheduled run picks it up.

To add a new source, repeat step 3: connect it the same way as the others, add it to the discovery answers, and mention it explicitly in the summarizer prompt's list of context.

If a run fails, check the collector and watcher output first, since they run without AI and are the most likely place for a broken login or expired token to show up as a loud, obvious error. If those succeeded but the board looks wrong or empty, check that the summarizer received all the context files; a common failure is a file path or folder name changing.

Expect a small, predictable cost for the summarizer step if you use a paid AI API, generally a few cents to a few tens of cents per day depending on volume. The collector and watcher scripts cost nothing beyond what you already pay for the underlying services.

## 9. Common failure modes

An expired or revoked token on one connector silently stops that source from contributing, and the board looks thinner than it should with no obvious error. The "not checked (reason)" rule exists specifically so this shows up on the board instead of disappearing quietly.

The host machine sleeps or loses its network connection at the scheduled run time, and the job does not fire or fires with no internet access. Confirm your scheduler is configured to wake the machine if needed, or accept that a missed run just means you see it at the next one.

The delivery surface goes stale because the "latest" board is not actually the one being displayed, an old cached document or a file that never got replaced. Always have the delivery step look for the newest one explicitly, and clean up old copies so there is no ambiguity.

The feedback loop gets out of sync: you mark something handled, but the next run's context files were already gathered before your feedback was saved, so it reappears once more before dropping. This is usually a step-ordering fix: read the feedback file after it is written, not before.

The summarizer quietly starts summarizing its own summaries instead of the primary sources, especially once a board has run for months and the "previous board" file is large. Periodically confirm items still cite real emails, messages, or documents, not just yesterday's board text.
