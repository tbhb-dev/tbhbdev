// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package appauth

import "net/http"

// HTTPClient exposes the client for tests.
func (s *Source) HTTPClient() *http.Client { return s.http }
