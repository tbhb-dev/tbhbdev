// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package ghclient

import (
	"net/http"
	"strings"
	"testing"
)

func FuzzNextLink(f *testing.F) {
	f.Add(`<https://api.test/x?page=2>; rel="next", <https://api.test/x?page=9>; rel="last"`)
	f.Add(`<https://api.test/x?a=1,2>; rel="next"`)
	f.Add(`<`)
	f.Add(`>; rel="next" <`)

	f.Fuzz(func(t *testing.T, value string) {
		got := nextLink(http.Header{"Link": {value}})
		if got != "" && !strings.Contains(value, "<"+got+">") {
			t.Fatalf("nextLink(%q) = %q, not a bracketed span of the header", value, got)
		}
	})
}

func FuzzRetryAfter(f *testing.F) {
	f.Add("120")
	f.Add("-1")
	f.Add("99999999999999999999")
	f.Add("Wed, 21 Oct 2015 07:28:00 GMT")
	f.Add(" 5 ")

	f.Fuzz(func(t *testing.T, value string) {
		got := retryAfter(http.Header{"Retry-After": {value}})
		if got < 0 || got > maxRetryAfter {
			t.Fatalf("retryAfter(%q) = %v, outside 0..%v", value, got, maxRetryAfter)
		}
	})
}
