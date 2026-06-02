---
name: setup-hubspot
description: Connect the customer's HubSpot account, generate the Private App token with the right scopes, and configure all required company/contact properties. Use during customer onboarding to wire up HubSpot as the CRM target. Walks through Private App creation in the HubSpot UI, captures the token, sets it as both `.env` and GitHub secret, and triggers property auto-creation via the pipeline's pre-flight.
---

# Set up HubSpot for a customer

## What this does

Walks the customer (or SE) through:
1. Creating a HubSpot Private App with the required scopes
2. Capturing the access token
3. Setting it as `.env` + GitHub secret
4. Triggering `is_icp_fit` property auto-creation
5. Verifying the other 4 `website_*` properties exist (auto-pre-flight in the pipeline)

See `ARCHITECTURE.md` §3.2 and §5 Step 4.

## Prerequisites

- The customer has a HubSpot account (any tier — free tier works, but EU-based portals use `app-eu1.hubspot.com`)
- The user has Super Admin or "Manage Private Apps" permission in HubSpot
- `gh` CLI authenticated

## Steps

1. **Ask the user**: are they on a US, EU, or other regional HubSpot portal? (Visible in the URL when they open HubSpot. E.g., `app.hubspot.com` vs `app-eu1.hubspot.com`.) The Private App token's prefix reveals this too (`pat-na1-...` for US, `pat-eu1-...` for EU).

2. **Find the HubSpot Portal ID**: in any HubSpot URL after `/contacts/` — typically a 7-9 digit number. Save for later.

3. **Walk the user through Private App creation**:
   - In HubSpot: **Settings** (gear icon) → **Account Setup** → **Integrations** → **Private Apps**
   - Click **"Create a private app"**
   - **Name**: `Website Deanonymization Pipeline`
   - On the **Scopes** tab, check the following 5 scopes:
     - `crm.objects.companies.read`
     - `crm.objects.companies.write`
     - `crm.schemas.companies.read`
     - `crm.schemas.companies.write` (needed for auto-creating `is_icp_fit` property)
     - `crm.objects.contacts.read`
     - `crm.objects.contacts.write`
   - Click **"Create app"** → confirm
   - Click **"Show token"** → copy the `pat-<region>-...` token

4. **Test the token immediately**:
   ```bash
   curl -H "Authorization: Bearer <token>" \
     "https://api.hubapi.com/crm/v3/properties/companies" | python -m json.tool | head -20
   ```
   Should return a JSON list of company properties. If 401 → token scopes missing.

5. **Save the token to `.env`**:
   ```bash
   # Edit .env (or create if not present):
   # HUBSPOT_API_KEY=<the pat-... token>
   ```

6. **Save the portal ID to `.env`** (enables clickable Slack links):
   ```bash
   # HUBSPOT_PORTAL_ID=<numeric portal id>
   ```

7. **Set the LinkedIn property name**. HubSpot's default is `hs_linkedin_url`. Confirm it exists on the customer's portal:
   ```bash
   curl -s -H "Authorization: Bearer <token>" \
     "https://api.hubapi.com/crm/v3/properties/contacts" \
     | python -c "import json, sys; props = json.load(sys.stdin).get('results', []); print([p['name'] for p in props if 'linkedin' in p['name'].lower()])"
   ```
   Pick the most appropriate (typically `hs_linkedin_url`). Save:
   ```bash
   # HUBSPOT_LINKEDIN_PROPERTY=hs_linkedin_url
   ```

8. **Set the GitHub secrets** (using `echo -n` to avoid trailing-newline mistakes):
   ```bash
   echo -n "<the pat-... token>" | gh secret set HUBSPOT_API_KEY
   echo -n "hs_linkedin_url" | gh secret set HUBSPOT_LINKEDIN_PROPERTY
   echo -n "<portal id>" | gh secret set HUBSPOT_PORTAL_ID
   ```

9. **Trigger property auto-creation** by running Step 4 once. The pipeline's pre-flight (`ensure_icp_property` + `verify_target_properties`) will auto-create `is_icp_fit` and report any missing `website_*` properties:
   ```bash
   source venv/bin/activate
   python run_pipeline.py --only 4 --dry-run
   ```

   If any `website_*` properties are missing, the pipeline raises a clear error listing them. Create those manually in HubSpot UI:
   - **Settings** → **Properties** → **Company properties** → **Create property**
   - Required:
     | Name | Type |
     |---|---|
     | `website_visit_count` | Single-line text |
     | `website_last_visited` | Date and time picker |
     | `website_pages_visited` | Multi-line text |
     | `website_visit_intent` | Single-line text |

   `is_icp_fit` is auto-created by the pipeline as a Single checkbox.

10. **Re-run Step 4 dry-run** to confirm all properties exist:
    ```bash
    python run_pipeline.py --only 4 --dry-run
    ```
    Expected: clean log output, no errors about missing properties.

## Verification

```bash
# Confirm the token works:
curl -s -H "Authorization: Bearer <token>" \
  "https://api.hubapi.com/crm/v3/properties/companies/is_icp_fit" \
  | python -m json.tool | head -20
```

Should show the `is_icp_fit` property definition with `type: enumeration`, `fieldType: booleancheckbox`.

```bash
# Confirm all GitHub secrets are set:
gh secret list | grep -E 'HUBSPOT'
```

## Caveats

- HubSpot Private App tokens encode the region in the prefix. `config.py` auto-detects the right UI base (`app-eu1.hubspot.com` for EU, etc.) — so a customer on EU HubSpot gets correct Slack deep-links automatically.
- `crm.schemas.companies.write` is **required** for property auto-creation. Without it, the user has to manually create `is_icp_fit` in the UI.
- The `is_icp_fit` property is required by `verify_target_properties` once added to `TARGET_PROPERTIES` in `config.py`. If the pipeline reports missing, either create the property in HubSpot UI or grant the schemas.write scope.
- Token rotation: if HubSpot expires the token, re-create the Private App and update both `.env` (`HUBSPOT_API_KEY`) AND the GitHub secret. No other action needed.
- For multi-customer deployments, each customer needs their own token + portal ID. Skill setup-github-actions stores them per-repo as secrets.
