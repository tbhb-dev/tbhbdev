// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package receiver

import (
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"testing"
)

func FuzzParseSignature(f *testing.F) {
	f.Add([]byte("s"), "sha256="+strings.Repeat("ab", sha256.Size))
	f.Add([]byte("s"), "sha256="+strings.Repeat("AB", sha256.Size))
	f.Add([]byte(""), "sha256="+strings.Repeat("ab", sha256.Size))
	f.Add([]byte("s"), "sha1="+strings.Repeat("ab", 20))
	f.Add([]byte("s"), "sha256=zz")
	f.Add([]byte("s"), "")

	f.Fuzz(func(t *testing.T, secret []byte, header string) {
		digest, ok := parseSignature(secret, header)
		if !ok {
			if digest != nil {
				t.Fatalf("parseSignature(%q) failed but returned a digest", header)
			}

			return
		}

		if len(secret) == 0 || len(digest) != sha256.Size {
			t.Fatalf("parseSignature(%q, %q) accepted %d bytes", secret, header, len(digest))
		}

		if !strings.EqualFold(header, signaturePrefix+hex.EncodeToString(digest)) {
			t.Fatalf("parseSignature(%q) = %x, not the header's digest", header, digest)
		}
	})
}
