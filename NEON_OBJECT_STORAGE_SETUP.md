# Neon Object Storage setup

Bucket: `library-uploads` (private, production branch)

The Flask app uploads return evidence photos to Neon Object Storage using the S3-compatible API. It stores object keys (not local filesystem paths) in `return_record.photo_paths` and generates temporary signed URLs for viewing private images.

## Vercel Environment Variables

Use either the custom names below or the standard AWS_* names from Neon Connect. The app accepts both.

Custom:
- `NEON_STORAGE_ENDPOINT` = Neon Connect Storage endpoint
- `NEON_STORAGE_ACCESS_KEY` = Neon Connect access key
- `NEON_STORAGE_SECRET_KEY` = Neon Connect secret key
- `NEON_STORAGE_BUCKET` = `library-uploads`
- `NEON_STORAGE_REGION` = `us-east-2` (or the exact region shown by Neon Connect)

Or standard Neon/AWS names:
- `AWS_ENDPOINT_URL_S3`
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_REGION`
- `AWS_S3_BUCKET` (optional; otherwise `library-uploads` is used)

Do not commit secret keys to GitHub or send them in chat.
