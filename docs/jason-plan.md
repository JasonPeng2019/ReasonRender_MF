send spec transcript -> EverOS compacts into case; we add to each case,  a json of the movable variables. -> EverOS retrieves -> EverOS gives back case context + original spec + variables subbed (compact task context); render the spec 

Need an evaluation (criteria) of the spec difficulty -> move into spec -> implement spec; assume there should be some gap between spec and code (so the simpler the spec, the EVEN simpler the code)



Variables: 
Identifiers — function/class name, parameter names, module/file path
Types — arg/return types, especially when only the entity type changes (User → Order)
Domain entity name — the noun the whole task is built around (table name, resource name, error-message subject)
Field/attribute lists — the set of fields on the entity, which drives contract clauses and test cases 1:1
Constants & defaults — default values, status codes, error messages/exception types
Edge-case values — some are universal (None, empty string, negative int) and truly reusable; others are domain-specific (e.g. "email already registered") and must be re-derived per entity
External targets — import paths, framework decorators/boilerplate (e.g. a FastAPI route stub, an ORM base class) that repeat verbatim across an entire workload
Test assertion shape — the pattern (assert f(valid) == expected, f(invalid) raises X) is reusable even when the literal values aren't
> Can investigate further/rethink

