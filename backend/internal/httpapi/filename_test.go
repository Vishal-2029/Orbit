package httpapi

import "testing"

// The title is free text from an untrusted client and goes straight into a
// Content-Disposition header, so a double quote in it would end the header value
// early and a newline would let the caller append headers of their own.
func TestSafeFilenameCannotBreakTheHeader(t *testing.T) {
	for _, title := range []string{
		`my "quoted" 360`,
		"line\r\nX-Injected: yes",
		"tab\there",
		"nul\x00byte",
		`back\slash`,
		"../../etc/passwd",
		"C:\\Windows\\System32",
	} {
		got := safeFilename(title)
		for _, bad := range []rune{'"', '\\', '/', '\r', '\n', 0} {
			for _, r := range got {
				if r == bad {
					t.Errorf("safeFilename(%q) = %q, still contains %q",
						title, got, string(bad))
				}
			}
		}
	}
}

func TestSafeFilenameKeepsSomethingReadable(t *testing.T) {
	for _, c := range []struct{ in, want string }{
		{"Living room", "Living room"},
		{"my desk 2026", "my desk 2026"},
		{"  padded  ", "padded"},
		{"Kitchen (north)", "Kitchen (north)"},
	} {
		if got := safeFilename(c.in); got != c.want {
			t.Errorf("safeFilename(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestSafeFilenameAlwaysReturnsSomething(t *testing.T) {
	// A title that is entirely stripped must not produce an empty filename -
	// the browser would then save the file with no name at all.
	for _, title := range []string{"", "   ", "...", "///", "\x00\x01", "。。。"} {
		if got := safeFilename(title); got == "" {
			t.Errorf("safeFilename(%q) returned empty", title)
		}
	}
}

func TestSafeFilenameIsBounded(t *testing.T) {
	long := ""
	for i := 0; i < 500; i++ {
		long += "a"
	}
	if got := safeFilename(long); len(got) > 60 {
		t.Errorf("safeFilename kept %d characters; filesystems and headers "+
			"both dislike unbounded names", len(got))
	}
}
