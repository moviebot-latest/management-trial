# Final Book PDF Upload

## Add PDF flow
- Staff selects a PDF from phone/PC.
- `Analyze PDF` extracts title, author, ISBN, publication year, category and a short description when the PDF exposes usable text/metadata.
- Every detected field remains editable before Save.
- On Save, the original PDF is uploaded to Neon Object Storage under `books/<book_id>/...pdf`.
- If no separate cover photo is selected, the first PDF page is rendered as a PNG cover preview and uploaded to Neon Object Storage.
- If a separate cover photo is selected from Gallery/Album, that photo is used instead.
- PostgreSQL stores only the object paths (`pdf_path`, `cover_url`) plus normal book metadata.
- `/books/<id>/pdf` creates a signed URL for the private PDF.

## Required Vercel environment for uploads
Use the exact values from Neon Object Storage -> Connect -> `.env`:
- `NEON_STORAGE_ENDPOINT`
- `NEON_STORAGE_ACCESS_KEY`
- `NEON_STORAGE_SECRET_KEY`
- `NEON_STORAGE_BUCKET=library-uploads`
- `NEON_STORAGE_REGION` (if supplied; otherwise `us-east-2`)

The application also accepts the equivalent AWS-style variable names used by Neon.

## Important
PDF metadata extraction is best-effort. Scanned/image-only PDFs may not expose text, so the user can manually edit fields before saving. No claim of 100% automatic extraction is made.
