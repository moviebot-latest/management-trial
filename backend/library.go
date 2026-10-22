--- a/backend/library.go
+++ b/backend/library.go
@@
-	uid := s.UserID
+	uid := int64(s.UserID)
@@
-	} else if b.UserID > 0 && b.UserID != s.UserID {
+	} else if b.UserID > 0 && b.UserID != int64(s.UserID) {
