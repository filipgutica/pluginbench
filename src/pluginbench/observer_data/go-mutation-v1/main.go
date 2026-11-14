// Report M1 mutation candidate sites with Go's standard parser, as byte spans.
package main

import (
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
)

type failure struct {
	Path   string `json:"path"`
	Reason string `json:"reason"`
}

type site struct {
	Path     string `json:"path"`
	Operator string `json:"operator"`
	Start    int    `json:"start"`
	End      int    `json:"end"`
}

var operators = map[token.Token]string{
	token.LSS: "relational_boundary", token.LEQ: "relational_boundary",
	token.GTR: "relational_boundary", token.GEQ: "relational_boundary",
	token.EQL: "equality_negation", token.NEQ: "equality_negation",
	token.LAND: "boolean_connective", token.LOR: "boolean_connective",
}

func main() {
	if len(os.Args) == 2 && os.Args[1] == "--version" {
		fmt.Println("go-mutation-v1")
		return
	}
	rows, failures := []site{}, []failure{}
	for _, path := range os.Args[1:] {
		fset := token.NewFileSet()
		file, err := parser.ParseFile(fset, path, nil, parser.SkipObjectResolution)
		if err != nil {
			failures = append(failures, failure{path, err.Error()})
			continue
		}
		offset := func(pos token.Pos) int { return fset.PositionFor(pos, false).Offset }
		ast.Inspect(file, func(node ast.Node) bool {
			switch n := node.(type) {
			case *ast.BinaryExpr:
				if operator, ok := operators[n.Op]; ok {
					start := offset(n.OpPos)
					rows = append(rows, site{path, operator, start, start + len(n.Op.String())})
				}
			case *ast.IfStmt:
				rows = append(rows, site{path, "condition_negation", offset(n.Cond.Pos()), offset(n.Cond.End())})
			}
			return true
		})
	}
	if err := json.NewEncoder(os.Stdout).Encode(map[string]any{"sites": rows, "failures": failures}); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
}
