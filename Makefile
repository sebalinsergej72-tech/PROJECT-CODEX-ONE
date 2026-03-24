.PHONY: smartbrain-bootstrap smartbrain-bootstrap-deps polymarket-sidecar

smartbrain-bootstrap:
	./scripts/ops/bootstrap_smartbrain.sh

smartbrain-bootstrap-deps:
	SMARTBRAIN_BOOTSTRAP_INSTALL_DEPS=true ./scripts/ops/bootstrap_smartbrain.sh

polymarket-sidecar:
	cd services/polymarket-execution-rs && cargo run
