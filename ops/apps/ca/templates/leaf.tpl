{{- define "client" -}}
{{- if ne (len .SANs) 1 }}{{ fail "one client URI SAN required" }}{{ end -}}
{{- $san := index .SANs 0 -}}
{{- if or (ne $san.Type "uri") (not (regexMatch "^spiffe://tbhb[.]dev/[^/?#]+/[^/?#]+$" $san.Value)) -}}
{{ fail "client identity must be spiffe://tbhb.dev/<host>/<service>" }}
{{- end -}}
{
  "subject": {},
  "sans": [{"type": "uri", "value": {{ toJson $san.Value }}}],
  "keyUsage": ["digitalSignature"],
  "extKeyUsage": ["clientAuth"]
}
{{- end -}}
{{- if ne (len .SANs) 1 }}{{ fail "one token SAN required" }}{{ end -}}
{{- $san := index .SANs 0 -}}
{{- if eq $san.Type "uri" -}}
{{ template "client" . }}
{{- else if eq $san.Type "dns" -}}
{
  "subject": {},
  "sans": {{ toJson .SANs }},
  "keyUsage": ["digitalSignature"],
  "extKeyUsage": ["serverAuth"]
}
{{- else -}}
{{ fail "only a client URI or listener DNS SAN is permitted" }}
{{- end -}}
