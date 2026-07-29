#!/usr/bin/env bash

set -u

BATCH_SIZE=50
MAX_BATCHES=6
LOG_DIR="logs/historical_drafts"

mkdir -p "$LOG_DIR"

for batch in $(seq 1 "$MAX_BATCHES"); do
  timestamp=$(date +"%Y%m%d_%H%M%S")
  log_file="$LOG_DIR/batch_${batch}_${timestamp}.log"

  echo
  echo "=================================================="
  echo "Historical draft batch $batch of $MAX_BATCHES"
  echo "Batch size: $BATCH_SIZE"
  echo "Log: $log_file"
  echo "=================================================="

  output=$(
    python scripts/generate_drafts.py \
      --limit "$BATCH_SIZE" \
      --write-notion 2>&1
  )
  status=$?

  printf '%s\n' "$output" | tee "$log_file"

  if [ "$status" -ne 0 ]; then
    echo
    echo "Batch $batch failed with exit code $status."
    echo "Stopping safely. Completed drafts will not be repeated."
    exit "$status"
  fi

  if printf '%s\n' "$output" | grep -q "Summary: drafted=0"; then
    echo
    echo "No eligible undrafted papers remain."
    echo "Historical drafting backfill is complete."
    exit 0
  fi

  if [ "$batch" -lt "$MAX_BATCHES" ]; then
    echo
    echo "Waiting 20 seconds before the next batch..."
    sleep 20
  fi
done

echo
echo "Reached the safety limit of $MAX_BATCHES batches."
echo "Review the logs and Notion before running the script again."
