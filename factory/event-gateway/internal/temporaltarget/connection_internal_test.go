// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// cspell:words temporaltarget

package temporaltarget

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

const endpoint = `"host_port":"[::1]:7233","namespace":"Test.space_1-2"`

func TestParseConnection(t *testing.T) {
	t.Parallel()

	for raw, files := range map[string]*TLSFiles{
		`{` + endpoint + `}`:          nil,
		`{` + endpoint + `,"tls":{}}`: new(TLSFiles),
		`{` + endpoint + `,"tls":{"ca_file":"ca.pem","cert_file":"cert.pem","key_file":"key.pem"}}`: {
			CA: "ca.pem", Cert: "cert.pem", Key: "key.pem",
		},
	} {
		want := Connection{HostPort: "[::1]:7233", Namespace: "Test.space_1-2", TLS: files}
		got, err := ParseConnection(json.RawMessage(raw))

		if err != nil || !reflect.DeepEqual(got, want) {
			t.Errorf("ParseConnection(%s) = %+v, %v", raw, got, err)
		}
	}

	for _, raw := range []string{
		``, `null`, `[]`, `{`, `{` + endpoint + `,"private":"hidden-value"}`, `{"namespace":"a"}`,
		`{"host_port":42}`, `{"host_port":"hidden-value"}`, `{"host_port":":1"}`, `{"host_port":"host:0"}`,
		`{"host_port":"host:"}`, `{"host_port":"host:65536"}`, `{"host_port":"host:hidden-value"}`,
		`{"host_port":"host:-1"}`, `{"host_port":"host:+1"}`, `{"host_port":"host:1"}`,
		`{"host_port":"host:65535","namespace":null}`, `{"host_port":"host:1","namespace":"hidden-value!"}`,
		`{"host_port":"host:1","namespace":"` + strings.Repeat("a", 256) + `"}`,
		`{` + endpoint + `,"tls":null}`, `{` + endpoint + `,"tls":[]}`, `{` + endpoint + `,"tls":{"private":"hidden-value"}}`,
		`{` + endpoint + `,"tls":{"ca_file":""}}`, `{` + endpoint + `,"tls":{"ca_file":1}}`,
		`{` + endpoint + `,"tls":{"cert_file":null,"key_file":"hidden-value"}}`,
		`{` + endpoint + `,"tls":{"cert_file":"hidden-value"}}`, `{` + endpoint + `,"tls":{"key_file":"hidden-value"}}`,
	} {
		_, err := ParseConnection(json.RawMessage(raw))
		if err == nil || strings.Contains(err.Error(), "hidden-value") {
			t.Errorf("ParseConnection(%s) returned %v", raw, err)
		}
	}
}
