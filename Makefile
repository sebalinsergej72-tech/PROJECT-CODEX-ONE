.PHONY: smartbrain-bootstrap smartbrain-bootstrap-deps

smartbrain-bootstrap:
	./scripts/ops/bootstrap_smartbrain.sh

smartbrain-bootstrap-deps:
	SMARTBRAIN_BOOTSTRAP_INSTALL_DEPS=true ./scripts/ops/bootstrap_smartbrain.sh
