# Campaign Studio — deployable WhatsApp Cloud API web app

A responsive browser-based campaign dashboard for a single administrator. Contacts require a name, E.164 phone number, consent timestamp and source. Only opted-in contacts are sent approved templates. Live sends use Meta's official WhatsApp Cloud API.

## Deploy to Render (Docker)

1. Create a **private GitHub repository** and upload the contents of this folder (do not commit `.env` or real customer contact files).
2. In Render, create a **Web Service** from that repository, choose **Docker**, and set the environment variables listed below.
3. Set `ADMIN_PASSWORD` to a long unique password and `SESSION_SECRET` to a randomly generated string of at least 32 characters. Set `COOKIE_SECURE=true` for HTTPS.
4. Attach a **persistent disk** mounted at `/app/data` and set `DATABASE_PATH=/app/data/campaigns.db`. A paid plan with persistent disk is necessary to retain SQLite data on Render. Without persistent storage, contacts and history may disappear on redeployment.
5. Deploy. Render supplies an HTTPS URL. Log in with `ADMIN_PASSWORD`.
6. In Meta Developers, set up WhatsApp Cloud API, an API access token and phone number ID. Add `WHATSAPP_ACCESS_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID` to Render environment variables.
7. Create an approved marketing template in WhatsApp Manager. If using the **name** variable, the template must have exactly one body variable `{{1}}`. Otherwise choose **No variables**.
8. For delivery statuses and automatic STOP processing, configure a Meta webhook callback URL `https://YOUR-DOMAIN/webhook`, set `WHATSAPP_VERIFY_TOKEN` (same value in Meta), set `META_APP_SECRET` to your Meta app secret, and subscribe to WhatsApp messages webhooks.
9. Upload a CSV with columns `name,phone,consent_at,consent_source,opted_in`. Use ISO 8601 timestamps for consent; use `yes` only for contacts who explicitly agreed to WhatsApp marketing. Test first with dry-run mode.

Example contact row: `Ayesha,+8801712345678,2026-10-01T10:30:00+06:00,Website marketing checkbox,yes` (illustrative only).

## Local developer run

```
pip install -r requirements.txt
export ADMIN_PASSWORD='your-strong-password'
export SESSION_SECRET='a-random-secret-with-at-least-32-characters'
export COOKIE_SECURE=false
uvicorn app:app --reload
```

Open http://127.0.0.1:8000.

## Important production limitations

- **Starter web app, not enterprise-grade**. Single-admin password; no user accounts, MFA, or password reset. Add MFA, stronger login rate limiting, managed database backups, audit logs, and a background job queue for production use.
- Each campaign processes **up to 50 opted-in contacts**; synchronous sends can time out on hosting. Do not increase without a proper queue and Meta rate-limit handling.
- Campaigns are **not scheduled**; manually initiated only.
- Contacts cannot be re-subscribed in the UI after opting out. This is intentional; obtain fresh documented consent before restoring a contact.
- Incoming STOP/unsubscribe/opt out/cancel (and Bengali বন্ধ) automatically opt out known contacts when webhook configured. Other variations and interactive replies may require additional logic.
- Webhook status is tracked only for matching Meta message IDs. Meta can send duplicate events; last event is shown, and out-of-order events are not resolved.
- Use official Meta policies: approved templates, explicit consent, proper unsubscribe handling, and no purchased/scraped lists. Meta may restrict messaging based on account status and limits. Message pricing is separate from hosting.
- Do not upload real patient data or other sensitive information unless you have appropriate authorization and safeguards.


## Phone-only spreadsheet import update
Import `.csv`, `.xlsx`, `.xls`, `.xlsm`, or `.xlsb` with a single phone-number column, optionally headed `phone`. Multi-column files need a recognizable phone header. The importer validates numbers, converts Bangladesh local mobile numbers to international format, and ignores duplicates. The administrator must confirm that recipients have opted in before importing. This confirmation is not a substitute for obtaining consent. Existing database schema is unchanged.
