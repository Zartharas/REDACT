#!/bin/bash
# Task #52: validate a real cloud load balancer, closing the exact
# question run_floci_elbv2_test.sh's own live run left open. That
# script found floci's ELB v2 to be control-plane-only: every
# `aws elbv2`/`aws ec2` API call succeeded with real ARNs, but target
# health stayed stuck at `initial` and a real HTTP request through the
# ALB's own DNS name never got a response -- confirming floci's own
# documented "In-process" (not "Real Docker") classification for this
# specific service. This script asks the identical two-part question
# (does the control plane work? does real traffic actually get
# proxied to backend targets?) against a real Azure Load Balancer
# instead.
#
# DELIBERATELY SCOPED DOWN from a full REDACT pipeline test, the same
# way run_floci_elbv2_test.sh's own Part 4 only ever checked /health
# (the one route exempt from REDACT_SERVICE_API_KEY auth), not real
# anonymization traffic: this script's backend targets are a minimal
# Python HTTP stub that returns 200 on GET /health, not the actual
# redact-service container. REDACT's own application logic (detection,
# anonymization, OpenSearch writes) is already verified elsewhere in
# this project at real scale (BUGS_AND_FIXES.md, ROADMAP.md item 9) --
# what's UNVERIFIED and what this script exists to test is narrowly
# "does a real cloud load balancer actually control-plane AND
# data-plane proxy traffic to real backend VMs," the same narrow
# question floci's ELB v2 test answered "no" to. Running the full
# REDACT stack on Azure VMs would answer a different, already-answered
# question at real extra cost and complexity for no new information.
#
# COST, disclosed plainly: 2x Standard_B1s VMs (~$0.0104/hr each) plus
# a Standard Load Balancer (~$0.025/hr plus a small per-GB data
#-processing charge) -- a short test run costs a few cents, trivial
# against Azure for Students' $100 credit, but NOT literally $0 the
# way Confluent's free-tier Basic cluster's first eCKU is. Nothing
# here is deleted automatically -- see the cleanup reminder at the end,
# which is not run automatically, the same convention as
# run_floci_elbv2_test.sh's own cleanup reminder.
#
# REQUIRES: Azure CLI (`az`) installed and already logged in
# (`az login`) -- this script does not attempt to run the interactive
# login flow itself. NOT run in this sandbox (no Azure CLI, no Azure
# account here) -- every `az` command's parameters checked against the
# real, documented Azure CLI syntax (az.md docs), not invented, but
# genuinely unverified until run live -- same disclosure as every other
# new script in this project before its first real run.
set -euo pipefail

if ! command -v az >/dev/null 2>&1; then
  echo "ERROR: Azure CLI ('az') not found. Install it first:" >&2
  echo "  macOS:   brew install azure-cli" >&2
  echo "  Other:   https://learn.microsoft.com/cli/azure/install-azure-cli" >&2
  exit 1
fi

if ! az account show >/dev/null 2>&1; then
  echo "ERROR: not logged in to Azure CLI. Run 'az login' first (opens a" >&2
  echo "browser to sign in with the same account as your Azure for" >&2
  echo "Students subscription), then rerun this script." >&2
  exit 1
fi

RG="redact-lb-test"
# LOCATION, made overridable 2026-09-06 after a real live run: "eastus"
# is disallowed for at least some Azure for Students subscriptions --
# Azure enforces a per-subscription "Allowed resource deployment
# regions" policy on these accounts (confirmed via live error:
# `RequestDisallowedByAzure`, "This policy maintains a set of best
# available regions where your subscription can deploy resources"),
# and the actual allowed list is subscription-specific, not a fixed
# public list. Find yours with:
#   az policy assignment list --output json | python3 -c \
#     "import sys,json; [print(a['displayName'], a.get('parameters',{}).get('listOfAllowedLocations',{}).get('value')) for a in json.load(sys.stdin) if 'listOfAllowedLocations' in a.get('parameters',{})]"
# then pass it as this script's first argument, e.g.:
#   ./run_azure_lb_test.sh westus2
LOCATION="${1:-eastus}"
# VM_SIZE, made overridable 2026-09-06 after a second real live finding:
# Standard_B1s hit `(SkuNotAvailable)` in northcentralus -- Azure's
# per-region SKU capacity fluctuates over time and isn't something a
# script can know in advance; this is a transient capacity restriction,
# not a config mistake. Standard_B2s is the fallback tried here (still
# a small, cheap burstable size, well within Azure for Students credit)
# -- if that also fails, try a different size or a different one of
# your allowed regions from Bug (a)'s discovery command above.
VM_SIZE="${2:-Standard_B2s}"
VNET="redact-lb-vnet"
SUBNET="redact-lb-subnet"
NSG="redact-lb-nsg"
LB="redact-lb"
BACKEND_POOL="redact-backend-pool"
PROBE="redact-health-probe"
LB_RULE="redact-lb-rule"
PUBLIC_IP="redact-lb-pip"

echo "=== Part 0: resource group (idempotent -- reuses if it already exists) ==="
az group create --name "$RG" --location "$LOCATION" --output none
echo "Resource group: $RG ($LOCATION)"

echo ""
echo "=== Part 1: network (VNet, subnet, NSG allowing 8080 inbound) ==="
az network vnet create --resource-group "$RG" --name "$VNET" \
    --address-prefix 10.10.0.0/16 --subnet-name "$SUBNET" \
    --subnet-prefix 10.10.1.0/24 --output none

az network nsg create --resource-group "$RG" --name "$NSG" --output none
az network nsg rule create --resource-group "$RG" --nsg-name "$NSG" \
    --name allow-health-port --priority 100 --direction Inbound \
    --access Allow --protocol Tcp --destination-port-ranges 8080 \
    --output none

echo ""
echo "=== Part 2: public IP + Standard Load Balancer (control-plane test, part A) ==="
# Standard SKU required -- Basic SKU LBs are being retired by Azure and
# don't support zone-redundant backend pools; Standard is the current,
# documented default for a new LB as of this project's own research
# (2026-09-05).
az network public-ip create --resource-group "$RG" --name "$PUBLIC_IP" \
    --sku Standard --allocation-method Static --output none

az network lb create --resource-group "$RG" --name "$LB" --sku Standard \
    --public-ip-address "$PUBLIC_IP" \
    --backend-pool-name "$BACKEND_POOL" --output none

az network lb probe create --resource-group "$RG" --lb-name "$LB" \
    --name "$PROBE" --protocol Http --port 8080 --path /health \
    --interval 5 --threshold 2 --output none

az network lb rule create --resource-group "$RG" --lb-name "$LB" \
    --name "$LB_RULE" --protocol Tcp --frontend-port 8080 \
    --backend-port 8080 --frontend-ip-name LoadBalancerFrontEnd \
    --backend-pool-name "$BACKEND_POOL" --probe-name "$PROBE" \
    --output none

LB_IP=$(az network public-ip show --resource-group "$RG" --name "$PUBLIC_IP" \
    --query 'ipAddress' --output tsv)
echo "Load balancer public IP: $LB_IP"

echo ""
echo "=== Part 3: two backend VMs running a minimal /health stub ==="
# Cloud-init, not a full REDACT deployment -- see this script's own
# header comment for why. A single inline Python http.server subclass
# is enough to answer "does the LB actually reach a real backend
# process on a real VM," which is the specific, narrow thing under
# test here.
CLOUD_INIT=$(cat <<'EOF'
#cloud-config
runcmd:
  - |
    cat > /opt/health_stub.py <<'PYEOF'
    import http.server, socket
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(f"OK from {socket.gethostname()}".encode())
            else:
                self.send_response(404)
                self.end_headers()
    http.server.HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
    PYEOF
  - nohup python3 /opt/health_stub.py > /var/log/health_stub.log 2>&1 &
EOF
)

CLOUD_INIT_FILE=$(mktemp)
echo "$CLOUD_INIT" > "$CLOUD_INIT_FILE"
trap 'rm -f "$CLOUD_INIT_FILE"' EXIT

# Pre-flight SKU check, added 2026-09-06 after Standard_B1s AND
# Standard_B2s both hit (SkuNotAvailable) in northcentralus back to
# back. Per Microsoft's own troubleshooting doc
# (learn.microsoft.com/azure/azure-resource-manager/troubleshooting/
# error-sku-not-available), `az vm list-skus` is the documented way to
# check whether a size is usable in a region/subscription BEFORE
# calling `az vm create` -- but that same doc is explicit that this
# only reports subscription/region-level restrictions, NOT real-time
# capacity: "there is no Azure API that exposes real-time VM capacity
# availability prior to deployment." So this check narrows the search
# to sizes with zero *known* restrictions -- it cannot guarantee the
# create call will succeed, only rule out sizes guaranteed to fail.
# Also checks quota via `az vm list-usage`, since a zero-quota VM
# family produces a similarly-worded failure that isn't a capacity
# issue at all and needs a different fix (a quota increase request).
echo ""
echo "Pre-flight: checking which VM sizes have no known restriction in ${LOCATION}..."
az vm list-skus --location "$LOCATION" --size "$VM_SIZE" --all --output table || true
echo ""
echo "Unrestricted sizes in ${LOCATION} (informational; doesn't guarantee live capacity):"

# Real finding, 2026-09-06: this pre-flight check itself showed
# Standard_B1s AND Standard_B2s both listed as
# "NotAvailableForSubscription, type: Location" for this specific
# subscription in northcentralus -- an actual documented restriction,
# not just transient capacity as first assumed. Rather than cost
# another manual round-trip guessing a third size by hand, build a
# real candidate list from the sizes this check reports as genuinely
# unrestricted, and try them in order automatically below. ARM64
# sizes (Azure's "p"-suffixed B-series, e.g. Standard_Bpls_v2 --
# Ampere Altra/Cobalt silicon) are filtered out since `--image
# Ubuntu2204` resolves to an x64 image and isn't guaranteed
# architecture-compatible with them.
mapfile -t UNRESTRICTED_SIZES < <(
    az vm list-skus --location "$LOCATION" --resource-type virtualMachines \
        --query "[?length(restrictions)==\`0\`].name" --output tsv 2>/dev/null \
        | grep -vE '^Standard_B[0-9]+p' \
        | head -20
)
printf '%s\n' "${UNRESTRICTED_SIZES[@]}"
echo ""

# Candidate list this run will actually try, in order: the
# user-requested/default $VM_SIZE first (respects an explicit
# override), then the unrestricted sizes above, de-duplicated.
CANDIDATE_SIZES=()
for s in "$VM_SIZE" "${UNRESTRICTED_SIZES[@]}"; do
    dup=false
    for existing in "${CANDIDATE_SIZES[@]:-}"; do
        [ "$existing" = "$s" ] && dup=true && break
    done
    [ "$dup" = false ] && CANDIDATE_SIZES+=("$s")
done
echo "Will try, in order: ${CANDIDATE_SIZES[*]}"
echo ""

# Real bug, found live 2026-09-06: `az vm create` has no `--lb`/
# `--backend-pool-name` flags at all -- those exist only on
# `az vmss create` (scale sets), not single VMs. I misremembered/
# invented this shape without checking it against the real, documented
# `az vm create` parameter list first, which is exactly the kind of
# mistake this project's own discipline (BUGS_AND_FIXES.md) exists to
# catch and record rather than paper over. The correct, documented
# pattern for a single VM: create its NIC explicitly in the target
# subnet, attach that NIC's IP configuration to the LB's backend pool
# via `az network nic ip-config address-pool add`, THEN create the VM
# referencing the NIC by name (`--nics`) instead of `--vnet-name`/
# `--subnet`/`--nsg` (those become the NIC's settings instead, since a
# VM created with an explicit `--nics` list takes its networking from
# the NIC(s), not from separate VM-level networking flags).
for i in 1 2; do
    VM_NAME="redact-backend-${i}"
    NIC_NAME="redact-backend-${i}-nic"

    echo "Creating NIC ${NIC_NAME}..."
    az network nic create --resource-group "$RG" --name "$NIC_NAME" \
        --vnet-name "$VNET" --subnet "$SUBNET" \
        --network-security-group "$NSG" \
        --output none

    echo "Attaching ${NIC_NAME} to the load balancer's backend pool..."
    # ipconfig1 is the default IP-configuration name `az network nic
    # create` assigns -- not invented, this is Azure CLI's own
    # documented default for a NIC's first (and here, only) IP config.
    az network nic ip-config address-pool add --resource-group "$RG" \
        --nic-name "$NIC_NAME" --ip-config-name ipconfig1 \
        --lb-name "$LB" --address-pool "$BACKEND_POOL" \
        --output none

    # Try each candidate size in order (see the pre-flight block above)
    # instead of a single hardcoded/overridden size -- added 2026-09-06
    # after Standard_B1s and Standard_B2s both came back
    # NotAvailableForSubscription in northcentralus on consecutive live
    # runs, which cost two separate manual reruns to discover one at a
    # time. `az vm create` failing is caught here (not fatal under
    # `set -e`, since a failing command inside an `if` condition
    # doesn't trigger it) so the loop can fall through to the next
    # candidate.
    VM_CREATED=false
    for SIZE_TRY in "${CANDIDATE_SIZES[@]}"; do
        echo "Creating ${VM_NAME} (size: ${SIZE_TRY})..."
        if az vm create --resource-group "$RG" --name "$VM_NAME" \
            --image Ubuntu2204 --size "$SIZE_TRY" \
            --nics "$NIC_NAME" \
            --custom-data "$CLOUD_INIT_FILE" \
            --generate-ssh-keys \
            --output none 2>/tmp/redact_vm_create_err.log; then
            echo "Succeeded with size ${SIZE_TRY}."
            # Lock this size in as the first candidate for the next VM
            # in the loop, so both backends end up the same size
            # rather than re-negotiating from scratch each time.
            VM_SIZE="$SIZE_TRY"
            CANDIDATE_SIZES=("$SIZE_TRY" "${CANDIDATE_SIZES[@]}")
            VM_CREATED=true
            break
        else
            echo "Size ${SIZE_TRY} failed -- trying next candidate. Error tail:"
            tail -5 /tmp/redact_vm_create_err.log
        fi
    done
    rm -f /tmp/redact_vm_create_err.log
    if [ "$VM_CREATED" = false ]; then
        echo ""
        echo "All candidate sizes failed for ${VM_NAME} in ${LOCATION}. This region may be"
        echo "genuinely capacity- or quota-constrained for this subscription right now."
        echo "Next step: try a different allowed region from Bug (a)'s discovery command"
        echo "in BUGS_AND_FIXES.md, e.g.:"
        echo "  ./run_azure_lb_test.sh westus"
        exit 1
    fi
done
echo "Both VMs created and attached to the load balancer's backend pool."

echo ""
echo "=== Part 4: waiting for health probes to pass (up to 3 minutes) ==="
# Cloud-init needs real time to run on first boot -- not instantaneous
# the way floci's local containers are. `az network lb show-backend-health`
# is the direct Azure-CLI equivalent of run_floci_elbv2_test.sh's own
# `aws elbv2 describe-target-health` check -- real per-target probe
# state, not just static backend-pool membership, which is exactly the
# distinction that mattered when floci's targets showed "registered"
# but stuck at "initial" forever. Printed raw rather than parsed to a
# single number -- this project's own discipline is to not invent an
# unverified JSON field-path shape for a command whose exact output
# structure hasn't been checked against a live Azure account; read the
# printed JSON yourself each poll rather than trust a parse that could
# be silently wrong.
for i in $(seq 1 6); do
    echo "  --- poll $i (look for \"Healthy\" in the output below) ---"
    az network lb show-backend-health --resource-group "$RG" --name "$LB" \
        --output json 2>/dev/null || echo "  (not ready yet)"
    sleep 15
done

echo ""
echo "=== Part 5: data-plane test -- does the LB's public IP actually proxy real traffic? ==="
echo "Attempting a real HTTP request through the load balancer's public IP..."
for attempt in 1 2 3 4 5; do
    if RESPONSE=$(curl -sf -m 10 "http://${LB_IP}:8080/health"); then
        echo ""
        echo "RESULT: Azure Load Balancer DOES proxy real traffic -- response: \"$RESPONSE\""
        echo "This is the full data-plane confirmation floci's ELB v2 test could NOT get --"
        echo "a real cloud load balancer control-plane AND data-plane both verified working."
        break
    fi
    echo "  attempt $attempt: no response yet, retrying in 15s (cloud-init may still be running)..."
    sleep 15
    if [ "$attempt" -eq 5 ]; then
        echo ""
        echo "RESULT: request through the load balancer's public IP did not succeed after"
        echo "5 attempts. Check: az vm run-command invoke --resource-group $RG --name"
        echo "redact-backend-1 --command-id RunShellScript --scripts 'cat /var/log/health_stub.log'"
        echo "to see whether the health stub actually started on the backend VM."
    fi
done

echo ""
echo "=== IMPORTANT: cleanup (not run automatically) ==="
echo "Deleting the whole resource group removes everything created above in one step"
echo "(both VMs, the load balancer, the public IP, the VNet/NSG):"
echo ""
echo "  az group delete --resource-group $RG --yes --no-wait"
echo ""
echo "Do this once you've confirmed the result above, pass or fail -- these resources"
echo "keep costing (a few cents/hour) until deleted, same principle as the Confluent"
echo "Cloud cleanup reminder from Task #53."
