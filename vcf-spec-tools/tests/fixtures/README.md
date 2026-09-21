# Test fixtures

## `known-good-9.1.0-three-node-vsan-esa.json`

A working three-node vSAN ESA VCF spec, vendored from
`lamw/vcf-91-in-box` (`config/three-node-vsan-esa.json`), used as the
structural reference for what a deployable spec contains.

**It is `version: 9.1.0.0`; this package targets 9.1.1.0.** Use it for
structure and semantics, never as a literal template. Verified example of
the difference: in 9.1.0 the LCM hostnames live in `fleetLcmSpec.hostname`
and `sddcLcmSpec.hostname`; in the 9.1.1 schema those definitions declare
only `size` and `version`, and the names have moved to
`vspClusterSpec.fleetFqdn` and `.instanceFqdn`. Copying this file verbatim
produces an invalid 9.1.1 spec.

Where this file and `vcfspec/schemas/9.1.1.0/sddc-spec.schema.json`
disagree, the schema wins and the difference is recorded in
`docs/superpowers/specs/2026-09-21-vcf-deployable-spec-design.md`.
