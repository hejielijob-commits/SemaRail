/**
 * ECharts 5.6 exposes runtime subpaths through package exports without a
 * `types` condition. Re-export the package's published declaration surfaces so
 * strict NodeNext typechecking remains enabled while esbuild resolves runtime
 * modules normally.
 */
declare module 'echarts/core' {
  export * from 'echarts/types/dist/core'
}

declare module 'echarts/charts' {
  export * from 'echarts/types/dist/charts'
}

declare module 'echarts/components' {
  export * from 'echarts/types/dist/components'
}

declare module 'echarts/renderers' {
  export * from 'echarts/types/dist/renderers'
}
