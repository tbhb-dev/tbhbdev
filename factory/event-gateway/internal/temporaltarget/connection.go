// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// cspell:words temporaltarget

package temporaltarget

import (
	"encoding/json"
	"net"
	"regexp"
	"strconv"
)

// Connection holds the endpoint, namespace, and optional TLS file paths.
type Connection struct {
	TLS                 *TLSFiles
	HostPort, Namespace string
}

// TLSFiles holds optional CA and client certificate paths.
type TLSFiles struct{ CA, Cert, Key string }

var namespacePattern = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,255}$`)

// ParseConnection validates the connection settings without accessing files or the network.
func ParseConnection(raw json.RawMessage) (Connection, error) {
	object := readObject(raw, "host_port", "namespace", "tls")
	if object == nil {
		return Connection{}, configError("invalid connection object")
	}

	hostPort, namespace := readString(object["host_port"]), readString(object["namespace"])
	host, port, splitErr := net.SplitHostPort(hostPort)
	number, portErr := strconv.ParseUint(port, 10, 16)

	switch {
	case splitErr != nil, host == "", portErr != nil, number == 0:
		return Connection{}, configError("invalid host_port")
	case !namespacePattern.MatchString(namespace):
		return Connection{}, configError("invalid namespace")
	}

	connection := Connection{HostPort: hostPort, Namespace: namespace, TLS: nil}

	if rawTLS := object["tls"]; rawTLS != nil {
		files := readObject(rawTLS, "ca_file", "cert_file", "key_file")
		if files == nil {
			return Connection{}, configError("invalid TLS object")
		}

		for _, path := range files {
			if readString(path) == "" {
				return Connection{}, configError("invalid TLS file path")
			}
		}

		if (files["cert_file"] == nil) != (files["key_file"] == nil) {
			return Connection{}, configError("TLS cert_file and key_file must be set together")
		}

		connection.TLS = &TLSFiles{
			CA:   readString(files["ca_file"]),
			Cert: readString(files["cert_file"]),
			Key:  readString(files["key_file"]),
		}
	}

	return connection, nil
}
