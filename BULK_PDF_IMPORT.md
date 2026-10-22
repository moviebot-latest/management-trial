# Bulk PDF Book Import

The Add New Book PDF analyzer now detects numbered catalog entries such as:

1. Python Programming
2. Data Structures
...
30. Project Management

When 2+ catalog entries are detected, Analyze shows the separate-book list. Pressing Save creates one database `book` row per detected entry. Each row gets a new unique `BK####` Book ID. The source catalog page is cropped into an individual PDF object, and an embedded cover image is extracted when present; both are stored in Neon Object Storage under that book's folder. Existing exact title+author records are skipped. Duplicate ISBNs do not abort the whole import; the duplicate ISBN is left blank for that new record.

A normal single-book PDF still uses the normal auto-fill flow.
