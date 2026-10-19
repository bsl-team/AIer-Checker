// gofmt-shaped sample: tabs, no semicolons, attached braces, mixed case roles.
package main

import (
	"fmt"
	"strings"
)

const maxParts = 4

type Row struct {
	Name  string
	Count int
}

func (r Row) Label() string {
	return strings.ToLower(r.Name)
}

func splitParts(input string) []string {
	parts := strings.Split(input, ",")
	if len(parts) > maxParts {
		parts = parts[:maxParts]
	}
	return parts
}

func main() {
	for _, part := range splitParts("a,bb,ccc") {
		fmt.Println(Row{Name: part, Count: len(part)}.Label())
	}
}
