package backend

import (
	"encoding/hex"
	"testing"
)

func TestPasswordPolicy(t *testing.T) {
	if !validPassword("StrongPass1!") {
		t.Fatal("expected strong password to pass")
	}
	for _, p := range []string{"Sh1!", "alllowercase1!", "ALLUPPERCASE1!", "NoNumber!!", "NoSymbol123"} {
		if validPassword(p) {
			t.Fatalf("expected password %q to fail", p)
		}
	}
}

func TestPBKDF2SHA256RFC6070(t *testing.T) {
	got := pbkdf2SHA256([]byte("password"), []byte("salt"), 1, 32)
	want, _ := hex.DecodeString("120fb6cffcf8b32c43e7225256c4f837a86548c92ccc35480805987cb70be17b")
	if string(got) != string(want) {
		t.Fatalf("pbkdf2 mismatch: %x", got)
	}
}

func TestDepartmentCode(t *testing.T) {
	cases := map[string]string{"IT / Engineering": "ITE", "Human Resources": "HR", "unknown": "EMP"}
	for in, want := range cases {
		if got := departmentCode(in); got != want {
			t.Errorf("departmentCode(%q)=%q want %q", in, got, want)
		}
	}
}

func TestRandomOTPFormat(t *testing.T) {
	for i := 0; i < 100; i++ {
		otp := randomOTP()
		if len(otp) != 6 {
			t.Fatalf("OTP must be six digits, got %q", otp)
		}
		for _, r := range otp {
			if r < '0' || r > '9' {
				t.Fatalf("OTP contains non-digit: %q", otp)
			}
		}
	}
}

func TestEmailValidation(t *testing.T) {
	if !validEmail("person@example.com") {
		t.Fatal("expected valid email")
	}
	for _, v := range []string{"", "not-an-email", "person@localhost", "person @example.com"} {
		if validEmail(v) {
			t.Fatalf("expected invalid email %q", v)
		}
	}
}
