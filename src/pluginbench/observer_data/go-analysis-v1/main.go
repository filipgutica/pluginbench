// Parse spans with Go's standard parser; all scores come from pinned analyzer libraries.
package main

import (
	"encoding/json"
	"fmt"
	"github.com/fzipp/gocyclo"
	"github.com/uudashr/gocognit"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
)

type function struct {
	Path       string `json:"path"`
	Name       string `json:"name"`
	Line       int    `json:"line"`
	Col        int    `json:"col"`
	EndLine    int    `json:"end_line"`
	LineCount  int    `json:"line_count"`
	Cognitive  int    `json:"cognitive"`
	Cyclomatic int    `json:"cyclomatic"`
}

func main() {
	if len(os.Args) == 2 && os.Args[1] == "--version" {
		fmt.Println("go-analysis-v1 gocognit=1.3.0 gocyclo=0.6.0")
		return
	}
	rows := []function{}
	for _, path := range os.Args[1:] {
		fset := token.NewFileSet()
		file, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(2)
		}
		ast.Inspect(file, func(node ast.Node) bool {
			var fn *ast.FuncDecl
			name := ""
			switch n := node.(type) {
			case *ast.FuncDecl:
				fn = n
				name = n.Name.Name
				if n.Recv != nil {
					name = fmt.Sprintf("method@%d:%s", fset.Position(n.Pos()).Line, name)
				}
			case *ast.FuncLit:
				name = fmt.Sprintf("closure@%d:%d", fset.Position(n.Pos()).Line, fset.Position(n.Pos()).Column)
				fn = &ast.FuncDecl{Type: n.Type, Body: n.Body, Name: ast.NewIdent(name)}
			default:
				return true
			}
			start, end := fset.Position(node.Pos()), fset.Position(node.End())
			rows = append(rows, function{path, name, start.Line, start.Column, end.Line, end.Line - start.Line + 1, gocognit.Complexity(fn), gocyclo.Complexity(node)})
			return true
		})
	}
	if err := json.NewEncoder(os.Stdout).Encode(rows); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
}
