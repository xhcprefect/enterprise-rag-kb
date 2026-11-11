<template>
  <div class="job-usage" data-testid="regression-job-usage">
    <span class="usage-title">{{ fj('usage.title') }}</span>

    <p v-if="!summary.hasCalls" class="usage-unknown" data-testid="usage-no-calls">
      {{ fj('usage.noCalls') }}
    </p>
    <template v-else>
      <div class="usage-line" data-testid="usage-attempts">
        <span class="usage-label">{{ fj('usage.calls') }}</span>
        <span>{{ formatNumber(summary.attempts) }}</span>
        <span class="usage-detail">
          {{ fj('usage.succeeded') }} {{ formatNumber(usage.succeeded_attempts) }} ·
          {{ fj('usage.failed') }} {{ formatNumber(usage.failed_attempts) }} ·
          {{ fj('usage.cancelled') }} {{ formatNumber(usage.cancelled_attempts) }} ·
          {{ fj('usage.interrupted') }} {{ formatNumber(usage.interrupted_attempts) }} ·
          {{ fj('usage.started') }} {{ formatNumber(usage.started_attempts) }} ·
          {{ fj('usage.other') }} {{ formatNumber(usage.other_attempts) }}
        </span>
      </div>

      <div class="usage-line" data-testid="usage-tokens">
        <span class="usage-label">{{ fj('usage.tokens') }}</span>
        <template v-if="summary.tokensMeasured">
          <span>
            {{ fj('usage.input') }} {{ summary.inputTokens ?? fj('usage.unknown') }} ·
            {{ fj('usage.output') }} {{ summary.outputTokens ?? fj('usage.unknown') }} ·
            {{ fj('usage.total') }} {{ summary.totalTokens ?? fj('usage.unknown') }}
          </span>
          <span v-if="usage.cached_input_tokens != null" class="usage-detail">
            {{ fj('usage.cached') }} {{ formatNumber(usage.cached_input_tokens) }}
          </span>
        </template>
        <template v-else>
          <span class="usage-unknown">{{ fj('usage.total') }}: {{ fj('usage.notZero') }}</span>
          <span v-if="summary.knownInputTokens != null" class="usage-detail">
            {{ fj('usage.knownInput') }} {{ formatNumber(summary.knownInputTokens) }}
          </span>
          <span v-if="summary.knownOutputTokens != null" class="usage-detail">
            {{ fj('usage.knownOutput') }} {{ formatNumber(summary.knownOutputTokens) }}
          </span>
          <span class="usage-detail">
            {{ fj('usage.missing') }} {{ formatNumber(summary.missingAttempts) }} /
            {{ formatNumber(summary.attempts) }}
          </span>
        </template>
      </div>

      <div class="usage-line" data-testid="usage-cost">
        <span class="usage-label">{{ fj('usage.price') }}</span>
        <template v-if="summary.costComplete">
          <span>
            {{ fj('usage.totalCost') }} {{ summary.currency || '' }}
            {{ formatCostAmount(summary.totalCost) }}
          </span>
          <span class="usage-detail">
            {{ fj('usage.priced') }} {{ formatNumber(summary.pricedAttempts) }} /
            {{ formatNumber(summary.attempts) }}
          </span>
        </template>
        <template v-else-if="summary.currencyMismatch">
          <span class="usage-unknown">{{ fj('usage.multiCurrency') }}</span>
        </template>
        <template v-else>
          <span class="usage-unknown">{{ fj('usage.totalCost') }}: {{ fj('usage.unknown') }}</span>
          <span v-if="summary.knownCostSubtotal != null" class="usage-detail">
            {{ fj('usage.knownSubtotal') }} {{ summary.currency || '' }}
            {{ formatCostAmount(summary.knownCostSubtotal) }}
          </span>
          <span class="usage-detail">
            {{ fj('usage.unpriced') }} {{ formatNumber(summary.unpricedAttempts) }} ·
            {{ fj('usage.unfinished') }} {{ formatNumber(summary.unfinishedAttempts) }}
          </span>
        </template>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { FeedbackRegressionJobUsage } from '@/api/feedbackRegressionJobs'
import { useFeedbackRegressionJobsI18n } from '@/i18n/messages/feedbackRegressionJobs'
import { formatNumber } from '@/i18n/format'
import { formatCostAmount, summarizeUsage } from '@/utils/feedbackRegressionJobs'

const props = defineProps<{ usage: FeedbackRegressionJobUsage }>()

const { fj } = useFeedbackRegressionJobsI18n()
const summary = computed(() => summarizeUsage(props.usage))
</script>

<style scoped>
.job-usage {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface-soft);
  color: var(--text-2);
  font-size: 11.5px;
  line-height: 1.55;
}

.usage-title {
  color: var(--text-3);
  font-size: 11px;
  font-weight: 600;
}

.usage-line {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 2px 8px;
  min-width: 0;
}

.usage-label {
  color: var(--text-3);
  font-weight: 550;
}

.usage-detail {
  color: var(--text-3);
}

.usage-unknown {
  margin: 0;
  color: var(--warning);
}
</style>
