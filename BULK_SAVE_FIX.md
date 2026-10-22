# Bulk PDF Save Fix

The bulk catalog PDF analysis can detect multiple books, but normal HTML `required` validation on the Title and Author fields previously blocked the form submission because bulk imports intentionally leave those fields blank.

V16 removes browser-level required validation from those two fields. The backend still validates Title/Author for normal single-book saves. For bulk imports, the backend detects the catalog PDF and creates one database record per detected book.

After Analyze PDF shows "N separate books detected", pressing Save N Books now submits the form and reaches the bulk import branch.
