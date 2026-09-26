# Cloud account setup for Tasks #51-53

Runbook for closing out the last three open items in `ROADMAP.md`/the
task tracker: getting real cloud accounts in place (Task #51) so the
floci-emulated ELB v2 (Task #52) and Kafka/MSK (Task #53) prototypes can
be checked against real infrastructure once, the same way every other
"local emulation only" gap in this project has eventually been closed.

**Account creation, billing setup, and entering payment details are
things only you can do** — this is a plain runbook to follow, not
something that gets automated. Researched 2026-09-05 against each
provider's current official pages (not assumed from memory, since these
programs change terms and eligibility often); sources at the bottom.

---

## The one thing worth knowing before you start

**AWS Educate is not the right account for Task #52/#53**, even though
it's the obvious "student AWS credits" answer. It provisions a
restricted **AWS Educate Starter Account**: no IAM console, no full
billing dashboard, `us-east-1` only, roughly 75% of services available,
and it's built as a training sandbox, not a general-purpose account.
Creating a VPC, subnets, an ALB, target groups, and an MSK cluster (what
Task #52/#53 actually need) needs IAM and full service access that this
account type doesn't grant. Recommending it anyway would just waste a
signup — flagging it now instead.

**What this means concretely:**

| Task | Recommended path | Why |
|---|---|---|
| #52 (real load balancer) | **Azure for Students** → Azure Load Balancer | $100 credit, **no credit card required**, self-serve with a school email. Safest option — nothing can auto-bill you past the credit without you separately upgrading to Pay-As-You-Go. |
| #53 (real managed Kafka) | **Confluent Cloud free tier** (explicitly allowed by the task's own description as an alternative to real AWS MSK) | AWS MSK has **no free tier** — it bills per broker-hour from the moment a cluster exists, which is real, avoidable financial risk for a one-off validation test. Confluent Cloud's free tier is built for exactly this kind of use and doesn't need any of the three big clouds as a prerequisite. |
| AWS itself | Optional, not required for #52/#53 | Only needed if you specifically want the real-ALB test to be AWS's ALB rather than Azure's Load Balancer. A standard AWS account requires a real card by AWS's own policy — AWS Educate does not remove that requirement for the access level these tasks need. |
| GCP | Optional, not required for #52/#53 | GCP's student-specific offer (Google Skills credits) is lab-only, not usable for standing infrastructure like a real load balancer. The general $300/90-day new-account trial would work for a GCP Load Balancer test if you want a third data point, but it needs a card the same way a standard AWS account does. |

If the goal is just closing #52/#53 with the least new financial-risk
surface, **Azure for Students + Confluent Cloud closes both tasks without
ever entering a card number anywhere.** AWS/GCP accounts are worth having
for the broader "student credits across all three" goal you mentioned,
but they're not on the critical path for these two tasks specifically.

---

## Step by step

### 1. Azure for Students (covers Task #52)

1. Go to `azure.microsoft.com/free/students` and sign in with your
   school email (a Microsoft account tied to that email, or one you
   create with it).
2. Verify enrollment — Azure accepts a `.edu`-style school email
   directly, or a dated enrollment document if your school email doesn't
   auto-verify.
3. **No credit card is requested at any point in this flow** — that's
   the whole point of using this path over a standard Azure account.
4. You land with $100 in credit and a set of always-free service tiers.
5. **Before creating anything real:** Azure Portal → *Cost Management +
   Billing* → *Budgets* → create a budget on the subscription (suggest
   $10, alert at 50%/80%/100% of that, well under your $100 credit) so
   you get an email if something runs longer than intended. Azure for
   Students accounts can't accidentally convert to pay-as-you-go and
   auto-bill you past the credit without you explicitly upgrading the
   subscription — but the budget alert is still worth having so you
   *notice* if the credit is draining faster than expected, rather than
   finding out at the end.
6. Once that's in place, tell me and I'll adapt `run_floci_elbv2_test.sh`
   into a real-Azure-Load-Balancer version (Azure CLI instead of the
   `aws elbv2`/`aws ec2` calls it currently uses) for Task #52.

### 2. Confluent Cloud free tier (covers Task #53)

1. Go to `confluent.cloud` and sign up (student email not required for
   this one — it's a standalone free tier, not a student program).
2. Confluent's free tier includes a permanently-free "Basic" cluster
   tier plus an initial promotional credit on top — check the current
   numbers on their pricing page when you sign up, since these figures
   move and I'd rather you confirm the live number than trust a
   potentially-stale one from me.
3. Set a usage/spend alert in the Confluent Cloud console (under
   *Billing & payment*) the same way as the Azure budget above, even
   though the Basic tier is designed to stay free under normal test
   usage.
4. Once you have a cluster and bootstrap broker address, tell me and
   I'll adapt `run_floci_kafka_test.sh` into a real-Confluent-Cloud
   version (pointing `src/queue_consumer.py`'s existing Kafka consumer
   path, and Logstash's `redact-pipeline-kafka.conf` producer, at
   Confluent's real broker instead of floci's local Redpanda) for
   Task #53.

### 3. AWS Educate (optional, for learning/labs — not for #52/#53)

Still worth having for the broader "credits across all three providers"
goal, and genuinely free/no-card:

1. Register at `aws.amazon.com/education/awseducate` with your school
   email.
2. You get a training-oriented account with hands-on labs and (per AWS's
   current page) no stated credit-card requirement.
3. **Do not plan on this account for Task #52/#53** — see the table
   above. If a specific course or your institution's AWS Academy
   enrollment grants a *different*, less-restricted credit grant later,
   that could change this — worth rechecking with your department if
   this project ever specifically needs real AWS ALB/MSK rather than the
   Azure/Confluent substitutes above.

### 4. GCP (optional)

1. Standard signup at `cloud.google.com/free` gets the general new-account
   trial credit (currently $300/90 days per Google's own page — reconfirm
   at signup since this changes). Requires a card, though GCP does not
   auto-charge past the trial without you explicitly enabling billing
   upgrades.
2. Separately, `cloud.google.com/edu/students` covers Google Skills Boost
   lab credits specifically — useful for learning, not for standing
   infrastructure.
3. If you want a GCP Load Balancer as a third real-cloud data point
   beyond Azure, set a budget alert (Billing → *Budgets & alerts*) the
   same way as the other two before creating anything.

---

## What "confirmed against real infrastructure" will and won't mean

Both #52 and #53's own task descriptions already say this explicitly,
worth restating here so it isn't lost by the time we write it up:
confirming against one free-tier account, briefly, proves the
architecture holds under real network conditions and a real managed
control plane — it does **not** prove anything about production traffic
volume, multi-region behavior, or sustained cost at real scale. Same
honesty standard as every load-test entry in `BUGS_AND_FIXES.md`.

---

**Sources** (checked 2026-09-05):
- [AWS Educate](https://aws.amazon.com/education/awseducate/)
- [AWS Educate Starter Account limitations — GeeksforGeeks](https://www.geeksforgeeks.org/cloud-computing/aws-educate-starter-account/)
- [Azure for Students](https://azure.microsoft.com/en-us/free/students/)
- [Google Cloud for Education — Students](https://cloud.google.com/edu/students)
- [Get and redeem education credits — Google Cloud Billing docs](https://docs.cloud.google.com/billing/docs/how-to/edu-grants)
- [GitHub Student Developer Pack](https://education.github.com/pack)
