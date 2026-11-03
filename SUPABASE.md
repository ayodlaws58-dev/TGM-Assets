# Supabase setup

The application uses Supabase Postgres for records and Supabase Auth for login. The service-role key is used only by the Flask server; never expose it to browser code or commit it.

## Deploy on Render

1. Push the application changes to the GitHub repository and connect that repository in Render using **New → Web Service**.
2. Set **Language** to Python, **Build Command** to `pip install -r requirements.txt`, and **Start Command** to `gunicorn app:app`.
3. After the first deploy, copy the service's public `https://...onrender.com` URL. In Supabase, add `<service-url>/invite` to the allowed Auth redirect URLs.
4. Add these environment variables to the Render service. Keep the service-role key and secret private.

   | Variable | Value |
   | --- | --- |
   | `TGM_DATABASE_BACKEND` | `supabase` |
   | `SUPABASE_URL` | Project URL from Supabase API settings |
   | `SUPABASE_ANON_KEY` | Project publishable/anon key |
   | `SUPABASE_SERVICE_ROLE_KEY` | Project service-role/secret key |
   | `SUPABASE_AUTH_REDIRECT_URL` | `<service-url>/invite` |
   | `SECRET_KEY` | A long, random Flask session secret |

5. Save the environment settings and redeploy the service.

## Import CSV records

1. Configure a custom SMTP provider in Supabase under **Authentication → SMTP Settings**. Supabase's default email service only delivers to pre-authorized team addresses; it won't reliably invite your users.
2. Run `python scripts/import_csv_to_supabase.py --dry-run` locally to validate the CSV data without contacting Supabase.
3. Once the service is deployed and all environment variables are set locally for the importer, run `python scripts/import_csv_to_supabase.py` to invite existing users and import the records. Invited users follow the link to `/invite` to set a password. Existing local password hashes are deliberately not imported.

The import is safe to rerun for existing profiles and records. It upserts records without deleting data already in Supabase. Review the dry-run output first, and back up the CSV files before importing.

For local development or the legacy smoke test only, set `TGM_DATABASE_BACKEND=csv`. The default backend is Supabase, and missing Supabase configuration is reported as an error.
