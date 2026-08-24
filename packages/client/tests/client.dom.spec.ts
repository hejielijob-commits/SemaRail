// @vitest-environment jsdom

import { act, createElement } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const echartsMock = vi.hoisted(() => {
  const instance = {
    setOption: vi.fn(),
    resize: vi.fn(),
    dispose: vi.fn(),
  }
  return {
    init: vi.fn(() => instance),
    instance,
    use: vi.fn(),
  }
})

vi.mock('echarts/core', () => ({ init: echartsMock.init, use: echartsMock.use }))
vi.mock('echarts/charts', () => ({ BarChart: {}, LineChart: {}, PieChart: {} }))
vi.mock('echarts/components', () => ({
  GridComponent: {},
  LegendComponent: {},
  TitleComponent: {},
  TooltipComponent: {},
}))
vi.mock('echarts/renderers', () => ({ CanvasRenderer: {} }))

import { DataQueryRow, SemanticConsoleSidebarAction, type DataQueryResultBlock, type DataQueryViewProps } from '../src/client/index.js'
import replayMeta from './fixtures/query-result-v1.json'

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const mountedRoots = new Set<Root>()

function settled(meta: unknown): DataQueryResultBlock {
  return {
    kind: 'tool-result',
    callId: 'dom-call-1',
    content: [],
    isError: false,
    meta,
  }
}

function props(meta: unknown = replayMeta): DataQueryViewProps {
  return {
    callId: 'dom-call-1',
    toolName: 'data_query',
    block: settled(meta),
  }
}

function mount(meta: unknown = replayMeta): { readonly container: HTMLDivElement; readonly root: Root } {
  const container = document.createElement('div')
  document.body.append(container)
  const root = createRoot(container)
  mountedRoots.add(root)
  act(() => root.render(createElement(DataQueryRow, props(meta))))
  return { container, root }
}

function button(container: ParentNode, text: string): HTMLButtonElement {
  const match = [...container.querySelectorAll('button')].find(candidate => candidate.textContent === text)
  if (!(match instanceof HTMLButtonElement)) throw new Error(`button not found: ${text}`)
  return match
}

function click(target: Element): void {
  act(() => target.dispatchEvent(new MouseEvent('click', { bubbles: true })))
}

function tableRows(container: ParentNode): string[][] {
  return [...container.querySelectorAll('tbody tr')].map(row =>
    [...row.querySelectorAll('td')].map(cell => cell.textContent ?? ''))
}

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  for (const root of mountedRoots) act(() => root.unmount())
  mountedRoots.clear()
  document.body.replaceChildren()
  vi.restoreAllMocks()
})

describe('DataQueryRow browser interactions', () => {
  it('adds a safe semantic-console link to the result card', () => {
    const { container } = mount()
    const link = container.querySelector('[data-query-semantic-console]')
    expect(link).toBeInstanceOf(HTMLAnchorElement)
    expect(link?.getAttribute('href')).toBe('http://127.0.0.1:48763')
    expect(link?.getAttribute('target')).toBe('_blank')
    expect(link?.getAttribute('rel')).toBe('noopener noreferrer')
    expect(link?.getAttribute('referrerpolicy')).toBe('no-referrer')
    expect(link?.textContent).toBe('语义层管理')
  })

  it('keeps the sidebar action on the public slot with isolated styles', () => {
    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    mountedRoots.add(root)
    act(() => root.render(createElement(SemanticConsoleSidebarAction, { wide: false })))
    const link = container.querySelector('[data-wren-semantic-console-action]')
    expect(link).toBeInstanceOf(HTMLAnchorElement)
    expect(link?.getAttribute('aria-label')).toBe('语义层管理')
    expect(link?.getAttribute('title')).toBe('语义层管理')
    expect(link?.getAttribute('data-sidebar-wide')).toBe('false')
    expect(container.querySelector('style[data-wren-semantic-console-style]')?.textContent).toContain('[data-wren-semantic-console-action]')
  })

  it('mounts Chart by default, changes tabs, sorts rows, switches SQL, and copies the visible SQL', async () => {
    const writeText = vi.fn(async () => undefined)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    })
    const { container } = mount()

    expect(container.querySelector('[data-query-tab-panel="chart"]')).not.toBeNull()
    expect(container.querySelector('[data-query-chart-canvas="line"]')).not.toBeNull()
    expect(button(container, 'Chart').getAttribute('aria-selected')).toBe('true')
    expect([...container.querySelectorAll('[data-query-tab]')].map(tab => tab.textContent)).toEqual(['Chart', 'Table', 'SQL'])
    expect(container.querySelector('[role="tabpanel"]')?.getAttribute('aria-labelledby')).toBe(container.querySelector('[data-query-tab="chart"]')?.id)

    click(button(container, 'Table'))
    const revenueHeading = container.querySelector('[data-query-column-sort="revenue"]')
    expect(revenueHeading).toBeInstanceOf(HTMLButtonElement)
    click(revenueHeading!)
    const revenueCell = revenueHeading?.closest('th')
    expect(revenueCell?.getAttribute('aria-sort')).toBe('ascending')
    click(revenueHeading!)
    expect(revenueCell?.getAttribute('aria-sort')).toBe('descending')
    expect(tableRows(container)[0]).toEqual(['2026-08-18', 'east', '21.00'])

    click(button(container, 'Chart'))
    expect(container.querySelector('[data-query-tab-panel="chart"]')).not.toBeNull()
    expect(container.querySelector('[data-query-chart-canvas="line"]')).not.toBeNull()

    click(button(container, 'SQL'))
    expect(container.querySelector('[data-query-tab-panel="sql"]')).not.toBeNull()
    const sqlBlock = container.querySelector('[data-query-sql-code-block]')
    expect(sqlBlock?.querySelector('code[data-query-sql-code]')).not.toBeNull()
    expect(container.querySelector('style')?.textContent).toContain('white-space: pre-wrap')
    expect(sqlBlock?.getAttribute('data-query-sql-current')).toBe('semantic')
    expect(sqlBlock?.textContent).toBe(replayMeta.semanticSql)

    click(button(container, 'Native SQL'))
    expect(sqlBlock?.getAttribute('data-query-sql-current')).toBe('native')
    expect(sqlBlock?.textContent).toBe(replayMeta.nativeSql)
    await act(async () => button(container, 'Copy').dispatchEvent(new MouseEvent('click', { bubbles: true })))
    expect(writeText).toHaveBeenCalledOnce()
    expect(writeText).toHaveBeenCalledWith(replayMeta.nativeSql)
    expect(button(container, 'Copied')).toBeInstanceOf(HTMLButtonElement)
  })

  it('paginates a large preview after sorting the complete result set', () => {
    const largeMeta = {
      ...replayMeta,
      chart: undefined,
      stats: { ...replayMeta.stats, returnedRows: 45 },
      previewRows: Array.from({ length: 45 }, (_, index) => ({
        day: `2026-09-${String(index + 1).padStart(2, '0')}`,
        region: 'east',
        revenue: String(index),
      })),
    }
    const { container } = mount(largeMeta)
    expect(container.querySelector('[data-query-tab-panel="table"]')).not.toBeNull()
    expect(container.querySelectorAll('tbody tr')).toHaveLength(20)
    expect(container.querySelector('[data-query-page-label]')?.textContent).toBe('Page 1 of 3')
    expect(container.querySelector('[data-query-row-range]')?.textContent).toBe('1-20 of 45 rows')
    click(button(container, 'Next'))
    expect(container.querySelectorAll('tbody tr')).toHaveLength(20)
    expect(container.querySelector('[data-query-row-range]')?.textContent).toBe('21-40 of 45 rows')
    click(button(container, 'Next'))
    expect(container.querySelectorAll('tbody tr')).toHaveLength(5)
    expect(container.querySelector('[data-query-row-range]')?.textContent).toBe('41-45 of 45 rows')
    expect(button(container, 'Next').disabled).toBe(true)
    const revenueHeading = container.querySelector('[data-query-column-sort="revenue"]')
    click(revenueHeading!)
    expect(container.querySelector('[data-query-page-label]')?.textContent).toBe('Page 1 of 3')
    expect(tableRows(container)[0]?.[2]).toBe('0')
  })

  it('renders malformed settled metadata through the safe DOM fallback', () => {
    const { container } = mount({ ...replayMeta, schemaVersion: 999 })
    const fallback = container.querySelector('[data-dsh-wren-data-query="fallback"]')
    expect(fallback).not.toBeNull()
    expect(fallback?.getAttribute('role')).toBe('status')
    expect(fallback?.textContent).toContain('unsupported or invalid result metadata')
    expect(container.querySelector('[data-query-tabs]')).toBeNull()
  })

  it('initializes, updates, resizes, and disposes ECharts across mount and unmount', () => {
    const { container, root } = mount()
    click(button(container, 'Chart'))

    const canvas = container.querySelector('[data-query-chart-canvas="line"]')
    expect(echartsMock.init).toHaveBeenCalledOnce()
    expect(echartsMock.init).toHaveBeenCalledWith(canvas)
    expect(echartsMock.instance.setOption).toHaveBeenCalledOnce()
    expect(echartsMock.instance.setOption).toHaveBeenCalledWith(
      expect.objectContaining({ tooltip: { trigger: 'axis' } }),
      { notMerge: true, lazyUpdate: false },
    )

    act(() => window.dispatchEvent(new Event('resize')))
    expect(echartsMock.instance.resize).toHaveBeenCalledOnce()

    act(() => root.unmount())
    mountedRoots.delete(root)
    expect(echartsMock.instance.dispose).toHaveBeenCalledOnce()
    act(() => window.dispatchEvent(new Event('resize')))
    expect(echartsMock.instance.resize).toHaveBeenCalledOnce()
  })
})
