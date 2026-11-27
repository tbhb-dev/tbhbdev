// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// cspell:words eventfields missingkey

package eventfields

import (
	"bytes"
	"errors"
	"fmt"
	"strings"
	"text/template"
	"text/template/parse"
)

const (
	maxTemplateBytes = 512
	maxResultBytes   = 1000
	templateName     = "workflow ID"
)

// ErrRender identifies a workflow ID that could not be rendered.
var ErrRender = errors.New("render workflow ID")

var errTemplate = errors.New("invalid workflow ID template")

// Template is a validated workflow ID template.
type Template struct{ template *template.Template }

// ParseTemplate validates a workflow ID template and its field references.
func ParseTemplate(text string) (*Template, error) {
	if len(text) == 0 || len(text) > maxTemplateBytes {
		return nil, fmt.Errorf("%w: must be 1 to %d bytes", errTemplate, maxTemplateBytes)
	}

	// A name longer than the source keeps a definition from replacing the root.
	root := templateName + text

	trees, err := parse.Parse(root, text, "", "")
	if err != nil {
		return nil, fmt.Errorf("%w: %w", errTemplate, err)
	}

	if len(trees) != 1 {
		return nil, fmt.Errorf("%w: template definitions are forbidden", errTemplate)
	}

	tree := trees[root]

	var fields Fields

	err = validateNode(fields.values(), tree.Root)
	if err != nil {
		return nil, err
	}

	compiled := template.New(templateName).Option("missingkey=error")

	return &Template{template.Must(compiled.AddParseTree(templateName, tree))}, nil
}

// Render executes the template with the present event fields.
func (t *Template) Render(f Fields) (string, error) {
	data := f.values()
	for name := range data {
		if !f.Has(name) {
			delete(data, name)
		}
	}

	var result cappedBuffer

	err := t.template.Execute(&result, data)
	if err != nil && !errors.Is(err, errTooLong) {
		return "", fmt.Errorf("%w: %w", ErrRender, err)
	}

	if err != nil || result.Len() == 0 {
		return "", fmt.Errorf("%w: result must be 1 to %d bytes", ErrRender, maxResultBytes)
	}

	return result.String(), nil
}

var errTooLong = errors.New("result too long")

// cappedBuffer fails a write that would grow it past maxResultBytes.
type cappedBuffer struct{ bytes.Buffer }

func (b *cappedBuffer) Write(data []byte) (int, error) {
	if b.Len()+len(data) > maxResultBytes {
		return 0, errTooLong
	}

	// bytes.Buffer.Write never fails.
	_, _ = b.Buffer.Write(data)

	return len(data), nil
}

func (f *Fields) values() map[string]any {
	return map[string]any{
		"provider": f.Provider, "event": f.Event, "delivery": f.Delivery, "seq": f.Seq,
		"repository": f.Repository, "action": f.Action, "number": f.Number, "label": f.Label,
		"head_sha": f.HeadSHA, "sender": f.Sender,
	}
}

func validateNodes[N parse.Node](allowed map[string]any, nodes ...N) error {
	for _, node := range nodes {
		err := validateNode(allowed, node)
		if err != nil {
			return err
		}
	}

	return nil
}

func validateNode(allowed map[string]any, input parse.Node) error {
	switch node := input.(type) {
	case *parse.ListNode:
		if node != nil {
			return validateNodes(allowed, node.Nodes...)
		}
	case *parse.ActionNode:
		return validateNode(allowed, node.Pipe)
	case *parse.PipeNode:
		return validateNodes(allowed, node.Cmds...)
	case *parse.CommandNode:
		return validateNodes(allowed, node.Args...)
	case *parse.IfNode:
		return validateNodes[parse.Node](allowed, node.Pipe, node.List, node.ElseList)
	case *parse.RangeNode:
		return validateNodes[parse.Node](allowed, node.Pipe, node.List, node.ElseList)
	case *parse.WithNode:
		return validateNodes[parse.Node](allowed, node.Pipe, node.List, node.ElseList)
	case *parse.FieldNode:
		return validateField(allowed, node.Ident)
	case *parse.VariableNode:
		return validateField(allowed, node.Ident[1:])
	case *parse.ChainNode:
		return errors.Join(validateNode(allowed, node.Node), validateField(allowed, node.Field))
	case *parse.TemplateNode:
		return fmt.Errorf("%w: template invocations are forbidden", errTemplate)
	}

	return nil
}

func validateField(allowed map[string]any, parts []string) error {
	if len(parts) == 0 {
		return nil
	}

	name := strings.Join(parts, ".")
	if _, ok := allowed[name]; !ok {
		return fmt.Errorf("%w: unknown field %q", errTemplate, name)
	}

	return nil
}
