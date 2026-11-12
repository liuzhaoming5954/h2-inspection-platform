<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { init, use, type ECharts } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { Sample } from '../api'

use([LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])
const props = defineProps<{ samples: Sample[] }>()
const host = ref<HTMLDivElement>()
let chart: ECharts | undefined
let observer: ResizeObserver | undefined

function update() {
  chart?.setOption({
    color: ['#138a8e', '#96a6d6'],
    textStyle: { fontFamily: 'sans-serif', color: '#7c8996' },
    tooltip: { trigger: 'axis' },
    grid: { left: 40, right: 38, top: 29, bottom: 28 },
    legend: { top: 0, right: 0, itemHeight: 3, itemWidth: 14, textStyle: { color: '#637080', fontSize: 11 } },
    xAxis: { type: 'category', data: props.samples.map(s => s.label ?? s.point_id), boundaryGap: false, axisLine: { lineStyle: { color: '#e4eaf0' } }, axisTick: { show: false }, axisLabel: { fontSize: 11 } },
    yAxis: [
      { type: 'value', name: '℃', scale: true, splitNumber: 3, axisLabel: { fontSize: 10 }, splitLine: { lineStyle: { color: '#edf1f5', type: 'dashed' } } },
      { type: 'value', name: '%RH', scale: true, splitNumber: 3, axisLabel: { fontSize: 10 }, splitLine: { show: false } },
    ],
    series: [
      { name: '温度', type: 'line', smooth: true, symbolSize: 6, data: props.samples.map(s => s.temperature_c), lineStyle: { width: 2.5 }, areaStyle: { opacity: 0.07 } },
      { name: '湿度', type: 'line', yAxisIndex: 1, smooth: true, symbolSize: 6, data: props.samples.map(s => s.humidity_percent), lineStyle: { width: 2, type: 'dashed' } },
    ],
  })
}
onMounted(() => {
  if (!host.value) return
  chart = init(host.value)
  observer = new ResizeObserver(() => chart?.resize())
  observer.observe(host.value)
  update()
})
watch(() => props.samples, update, { deep: true })
onBeforeUnmount(() => { observer?.disconnect(); chart?.dispose() })
</script>

<template><div ref="host" class="environment-chart" aria-label="巡检点温湿度趋势图"></div></template>
