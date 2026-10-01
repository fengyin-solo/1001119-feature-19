<template>
  <section class="page grant-page" data-module="grant">
    <header class="page-head">
      <div>
        <h2>项目领用授权柜</h2>
        <p class="page-desc">
          按工程、施工路段与材料类别授予领用/只读权限，实时预览会影响的任务与库存；
          授权结论随流程落到材料台账、工程待办与车辆装载清单。越权领用一律拒绝，紧急抢险以指挥审批为准。
        </p>
      </div>
      <div class="page-actions identity">
        <label class="filter-item">
          <span>当前操作单位</span>
          <select v-model="identity.unit" @change="reloadAll">
            <option v-for="u in options.领用单位" :key="u" :value="u">{{ u }}</option>
          </select>
        </label>
      </div>
    </header>

    <div class="tab-bar">
      <button
        v-for="tab in enrichedTabs"
        :key="tab.key"
        class="tab-btn"
        :class="{ active: activeTab === tab.key }"
        type="button"
        @click="activeTab = tab.key"
      >
        {{ tab.label }}
        <em v-if="tab.badge" class="tab-badge">{{ tab.badge }}</em>
      </button>
    </div>

    <!-- 授权预览 -->
    <div v-if="activeTab === 'preview'" class="tab-panel">
      <form class="filter-bar" @submit.prevent="runPreview">
        <label class="filter-item">
          <span>工程</span>
          <select v-model="previewForm.project" @change="onPreviewProjectChange">
            <option value="">请选择工程</option>
            <option v-for="p in options.工程" :key="p.工程编号" :value="p.工程编号">{{ p.工程编号 }} · {{ p.工程名称 }}</option>
          </select>
        </label>
        <label class="filter-item">
          <span>施工路段</span>
          <input v-model="previewForm.section" placeholder="留空取工程登记路段" />
        </label>
        <label class="filter-item">
          <span>材料类别</span>
          <select v-model="previewForm.category">
            <option value="">请选择类别</option>
            <option v-for="c in options.材料类别" :key="c" :value="c">{{ c }}</option>
          </select>
        </label>
        <label class="filter-item">
          <span>领用单位</span>
          <select v-model="previewForm.unit">
            <option value="">仅预览任务/库存</option>
            <option v-for="u in options.领用单位" :key="u" :value="u">{{ u }}</option>
          </select>
        </label>
        <label class="filter-item">
          <span>拟领数量</span>
          <input v-model.number="previewForm.quantity" type="number" min="0" />
        </label>
        <button class="btn primary" type="submit">实时预览</button>
      </form>

      <div v-if="!preview" class="empty-state">选择工程、路段与材料类别后，实时预览会影响的任务与库存。</div>

      <template v-else>
        <div class="stat-row">
          <article class="stat-card">
            <span class="stat-label">受影响任务</span>
            <strong class="stat-value">{{ preview.受影响任务数 }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">类别可发库存</span>
            <strong class="stat-value">{{ preview.库存汇总.可发数量 }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">已预占</span>
            <strong class="stat-value">{{ preview.库存汇总.预占数量 }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">当前权限</span>
            <strong class="stat-value">
              <span v-if="preview.权限结论" :class="permClass(preview.权限结论.权限)">{{ preview.权限结论.权限 }}</span>
              <span v-else>—</span>
            </strong>
          </article>
        </div>

        <div v-if="preview.权限结论" class="decision-box">
          <div><b>权限结论：</b><span :class="permClass(preview.权限结论.权限)">{{ preview.权限结论.权限 }}</span></div>
          <div><b>来源：</b>{{ preview.权限结论.来源 }}{{ preview.权限结论.是否继承 ? '（默认继承工程归属）' : '' }}</div>
          <div><b>依据：</b>{{ preview.权限结论.授权依据 }}</div>
          <div v-if="preview.权限结论.权限 === '领用'">
            <b>额度：</b>授权 {{ preview.权限结论.授权额度 }} ／ 已用 {{ preview.权限结论.已用额度 }}
            ／ 预占 {{ preview.权限结论.预占额度 }} ／ <b>剩余 {{ preview.权限结论.剩余额度 }}</b>
          </div>
          <div v-if="preview.可行性" class="feasible" :class="{ ok: preview.可行性.结论 === '可领用' }">
            拟领 {{ preview.可行性.申请数量 }}：{{ preview.可行性.结论 }}
            （额度{{ preview.可行性.额度是否充足 ? '充足' : '不足' }}、库存{{ preview.可行性.库存是否充足 ? '充足' : '不足' }}）
          </div>
        </div>

        <h3 class="block-title">会影响的任务（{{ preview.受影响任务.length }}）</h3>
        <table class="data-table">
          <thead><tr><th>任务模块</th><th>编号</th><th>位置</th><th>状态</th><th>是否待办</th><th>关联工程</th></tr></thead>
          <tbody>
            <tr v-for="(t, i) in preview.受影响任务" :key="String(t.编号) + i">
              <td>{{ t.模块 }}</td><td>{{ t.编号 }}</td><td>{{ t['名称/位置'] }}</td>
              <td>{{ t.状态 }}</td>
              <td><span :class="t.是否待办 ? 'tag warn' : 'tag muted'">{{ t.是否待办 ? '待办' : '已完结' }}</span></td>
              <td>{{ t.关联工程 || '—' }}</td>
            </tr>
            <tr v-if="!preview.受影响任务.length"><td colspan="6" class="empty-state">该路段在这一材料类别下暂无关联任务</td></tr>
          </tbody>
        </table>

        <h3 class="block-title">会影响的库存（按类别汇总，预览不暴露供应商）</h3>
        <table class="data-table">
          <thead><tr><th>材料编号</th><th>材料名称</th><th>规格型号</th><th>单位</th><th>库存</th><th>已预占</th><th>可发</th><th>存放地点</th></tr></thead>
          <tbody>
            <tr v-for="m in preview.库存" :key="m.材料编号">
              <td>{{ m.材料编号 }}</td><td>{{ m.材料名称 }}</td><td>{{ m.规格型号 }}</td><td>{{ m.计量单位 }}</td>
              <td>{{ m.库存数量 }}</td><td>{{ m.预占数量 }}</td>
              <td><b :class="m.可发数量 > 0 ? 'num-ok' : 'num-bad'">{{ m.可发数量 }}</b></td>
              <td>{{ m.存放地点 }}</td>
            </tr>
          </tbody>
        </table>
      </template>
    </div>

    <!-- 授权规则 -->
    <div v-if="activeTab === 'grants'" class="tab-panel">
      <h3 class="block-title">新增 / 调整授权（工程 × 施工路段 × 材料类别 × 领用单位）</h3>
      <form class="grid-form" @submit.prevent="submitGrant">
        <label><span>工程</span>
          <select v-model="grantForm.工程编号" @change="syncGrantSection">
            <option value="">选择工程</option>
            <option v-for="p in options.工程" :key="p.工程编号" :value="p.工程编号">{{ p.工程编号 }}</option>
          </select>
        </label>
        <label><span>施工路段</span><input v-model="grantForm.施工路段" placeholder="须与工程登记一致" /></label>
        <label><span>材料类别</span>
          <select v-model="grantForm.材料类别">
            <option value="">选择类别</option>
            <option v-for="c in options.材料类别" :key="c" :value="c">{{ c }}</option>
          </select>
        </label>
        <label><span>领用单位</span><input v-model="grantForm.领用单位" list="unit-list" /></label>
        <datalist id="unit-list"><option v-for="u in options.领用单位" :key="u" :value="u" /></datalist>
        <label><span>权限</span>
          <select v-model="grantForm.权限">
            <option value="领用">领用</option><option value="只读">只读</option>
          </select>
        </label>
        <label><span>授权额度</span><input v-model.number="grantForm.授权额度" type="number" min="0" :disabled="grantForm.权限 === '只读'" /></label>
        <label><span>到期日期</span><input v-model="grantForm.到期日期" type="date" /></label>
        <label class="span-2"><span>备注</span><input v-model="grantForm.备注" /></label>
        <div class="form-actions span-2"><button class="btn primary" type="submit">保存授权</button></div>
      </form>

      <h3 class="block-title">授权规则（{{ grants.length }}）· 未列出的单位默认按工程归属继承</h3>
      <table class="data-table">
        <thead><tr><th>授权编号</th><th>工程</th><th>路段</th><th>材料类别</th><th>领用单位</th><th>权限</th><th>额度</th><th>已用</th><th>预占</th><th>来源</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="g in grants" :key="g.授权编号">
            <td>{{ g.授权编号 }}</td><td>{{ g.工程编号 }}</td><td>{{ g.施工路段 }}</td><td>{{ g.材料类别 }}</td>
            <td>{{ g.领用单位 }}</td>
            <td><span :class="permClass(g.权限)">{{ g.权限 }}</span></td>
            <td>{{ g.授权额度 }}</td><td>{{ g.已用额度 }}</td><td>{{ g.预占额度 }}</td>
            <td>{{ g.来源 }}</td><td>{{ g.状态 }}</td>
            <td class="row-actions">
              <button class="link" type="button" @click="toggleGrantState(g)">{{ g.状态 === '生效' ? '停用' : '启用' }}</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 紧急例外 -->
    <div v-if="activeTab === 'exception'" class="tab-panel">
      <h3 class="block-title">申请紧急抢险临时例外（最终以指挥审批为准）</h3>
      <form class="grid-form" @submit.prevent="submitException">
        <label><span>工程</span>
          <select v-model="excForm.工程编号" @change="syncExcSection">
            <option value="">选择工程</option>
            <option v-for="p in options.工程" :key="p.工程编号" :value="p.工程编号">{{ p.工程编号 }}</option>
          </select>
        </label>
        <label><span>施工路段</span><input v-model="excForm.施工路段" /></label>
        <label><span>材料类别</span>
          <select v-model="excForm.材料类别">
            <option value="">选择类别</option>
            <option v-for="c in options.材料类别" :key="c" :value="c">{{ c }}</option>
          </select>
        </label>
        <label><span>申请单位</span><input v-model="excForm.申请单位" list="unit-list" /></label>
        <label><span>申请数量</span><input v-model.number="excForm.申请数量" type="number" min="1" /></label>
        <label class="span-2"><span>抢险事由</span><input v-model="excForm.事由" placeholder="如：橙色预警积水抢险" /></label>
        <div class="form-actions span-2"><button class="btn primary" type="submit">提交例外申请</button></div>
      </form>

      <h3 class="block-title">临时例外（{{ exceptions.length }}）</h3>
      <table class="data-table">
        <thead><tr><th>例外编号</th><th>工程/路段</th><th>类别</th><th>申请单位</th><th>数量</th><th>事由</th><th>状态</th><th>审批</th><th>有效期</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="e in exceptions" :key="e.例外编号">
            <td>{{ e.例外编号 }}</td><td>{{ e.工程编号 }} / {{ e.施工路段 }}</td><td>{{ e.材料类别 }}</td>
            <td>{{ e.申请单位 }}</td><td>{{ e.申请数量 }}</td><td class="ellipsis">{{ e.事由 }}</td>
            <td><span :class="exceptionClass(e.状态)">{{ e.状态 }}</span></td>
            <td>{{ e.审批人 ? `${e.审批人}（${e.审批职务}）` : '—' }}</td>
            <td>{{ e.状态 === '已批准' ? `${e.生效时间} ~ ${e.失效时间}` : '—' }}</td>
            <td class="row-actions">
              <template v-if="e.状态 === '待指挥审批'">
                <button class="link" type="button" @click="approveExc(e, true)">指挥批准</button>
                <button class="link danger" type="button" @click="approveExc(e, false)">驳回</button>
              </template>
              <span v-else class="muted-text">已闭环</span>
            </td>
          </tr>
        </tbody>
      </table>
      <p class="hint">指挥批准需审批职务包含「指挥」；非指挥身份提交批准会被系统拒绝并保持待审批。</p>
    </div>

    <!-- 领料 -->
    <div v-if="activeTab === 'requisition'" class="tab-panel">
      <h3 class="block-title">发起领料（以当前操作单位身份，原子校验权限/额度/库存并预占）</h3>
      <form class="grid-form" @submit.prevent="submitRequisition">
        <label><span>工程</span>
          <select v-model="reqForm.工程编号" @change="syncReqSection">
            <option value="">选择工程</option>
            <option v-for="p in options.工程" :key="p.工程编号" :value="p.工程编号">{{ p.工程编号 }}</option>
          </select>
        </label>
        <label><span>材料编号</span><input v-model="reqForm.材料编号" placeholder="如 MATE-0001" /></label>
        <label><span>领用单位</span><input v-model="reqForm.领用单位" list="unit-list" /></label>
        <label><span>领用人</span><input v-model="reqForm.领用人" /></label>
        <label><span>数量</span><input v-model.number="reqForm.申请数量" type="number" min="1" /></label>
        <label><span>车牌号</span><input v-model="reqForm.车牌号" placeholder="可先留空派车" /></label>
        <label class="span-2"><span>用途</span><input v-model="reqForm.用途" /></label>
        <div class="form-actions span-2"><button class="btn primary" type="submit">申请并预占</button></div>
      </form>

      <h3 class="block-title">领用单（{{ requisitions.length }}）</h3>
      <table class="data-table">
        <thead><tr><th>单号</th><th>工程/路段</th><th>材料</th><th>单位</th><th>数量</th><th>授权依据</th><th>结论</th><th>状态</th><th>时间</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="r in requisitions" :key="r.领用单号" :class="{ rejected: r.状态 === '已拒绝' }">
            <td>{{ r.领用单号 }}</td><td>{{ r.工程编号 }} / {{ r.施工路段 }}</td>
            <td>{{ r.材料编号 }}{{ r.材料名称 ? ' · ' + r.材料名称 : '' }}</td>
            <td>{{ r.领用单位 }}</td><td>{{ r.申请数量 }}{{ r.计量单位 }}</td>
            <td class="ellipsis">{{ r.授权依据 }}</td>
            <td><span :class="conclusionClass(r.授权结论)">{{ r.授权结论 }}</span></td>
            <td>
              {{ r.状态 }}
              <div v-if="r.拒绝原因" class="reject-reason">{{ r.拒绝原因 }}</div>
            </td>
            <td>{{ r.申请时间 }}</td>
            <td class="row-actions">
              <button v-if="r.状态 === '已预占'" class="link" type="button" @click="issue(r)">确认发料</button>
              <span v-else class="muted-text">—</span>
            </td>
          </tr>
        </tbody>
      </table>
      <p class="hint">越权/超额/库存不足的申请会被拒绝；拒绝记录刻意不回传供应商等敏感信息。</p>
    </div>

    <!-- 落地：待办与装载清单 + 转组 -->
    <div v-if="activeTab === 'downstream'" class="tab-panel">
      <h3 class="block-title">工程转组（验证历史领用关系不随转组改变）</h3>
      <form class="filter-bar" @submit.prevent="transferProject">
        <label class="filter-item"><span>工程</span>
          <select v-model.number="transferForm.id">
            <option :value="0">选择工程</option>
            <option v-for="p in options.工程" :key="p.工程编号" :value="p.id">{{ p.工程编号 }}（当前归属：{{ p.归属单位 }}）</option>
          </select>
        </label>
        <label class="filter-item"><span>新归属单位</span><input v-model="transferForm.new_owner" list="unit-list" /></label>
        <button class="btn primary" type="submit">执行转组</button>
      </form>
      <p class="hint">转组后：后续权限按新归属继承；历史领用单的授权口径快照保持原值。</p>

      <h3 class="block-title">工程待办（{{ todos.length }}）</h3>
      <table class="data-table">
        <thead><tr><th>工程</th><th>事项</th><th>来源</th><th>关联单号</th><th>状态</th><th>时间</th></tr></thead>
        <tbody>
          <tr v-for="t in todos" :key="t.id">
            <td>{{ t.工程编号 }}</td><td>{{ t.事项 }}</td><td>{{ t.来源 }}</td><td>{{ t.关联单号 }}</td>
            <td><span :class="t.状态 === '待处理' ? 'tag warn' : 'tag ok'">{{ t.状态 }}</span></td><td>{{ t.时间 }}</td>
          </tr>
        </tbody>
      </table>

      <h3 class="block-title">车辆装载清单（{{ loads.length }}）</h3>
      <table class="data-table">
        <thead><tr><th>单号</th><th>领用单</th><th>车牌号</th><th>工程/路段</th><th>材料</th><th>数量</th><th>状态</th><th>时间</th></tr></thead>
        <tbody>
          <tr v-for="l in loads" :key="l.单号">
            <td>{{ l.单号 }}</td><td>{{ l.领用单号 }}</td><td>{{ l.车牌号 }}</td><td>{{ l.工程编号 }} / {{ l.施工路段 }}</td>
            <td>{{ l.材料编号 }} · {{ l.材料名称 }}</td><td>{{ l.数量 }}{{ l.计量单位 }}</td>
            <td><span :class="l.状态 === '已发运' || l.状态 === '已签收' ? 'tag ok' : 'tag warn'">{{ l.状态 }}</span></td>
            <td>{{ l.时间 }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <footer class="page-foot">
      <span v-if="message" :class="messageOk ? 'ok-text' : 'error-text'">{{ message }}</span>
      <span v-else>授权结论实时落到材料台账、工程待办与车辆装载清单</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

const BASE = '/api/grant'

type Permission = '领用' | '只读' | '无权限'
interface OptionSet {
  工程: { id: number; 工程编号: string; 工程名称: string; 施工路段: string; 归属单位: string; 状态: string }[]
  材料类别: string[]
  领用单位: string[]
  权限: string[]
}
interface Preview {
  施工路段: string
  受影响任务: { 模块: string; 编号: string; '名称/位置': string; 状态: string; 是否待办: boolean; 关联工程: string }[]
  受影响任务数: number
  库存汇总: { 库存数量: number; 预占数量: number; 可发数量: number }
  库存: { 材料编号: string; 材料名称: string; 规格型号: string; 计量单位: string; 库存数量: number; 预占数量: number; 可发数量: number; 存放地点: string }[]
  权限结论: null | { 权限: Permission; 来源: string; 是否继承: boolean; 授权依据: string; 授权额度: number; 已用额度: number; 预占额度: number; 剩余额度: number }
  可行性: null | { 申请数量: number; 额度是否充足: boolean; 库存是否充足: boolean; 结论: string }
}
type Row = Record<string, any>

const tabs = [
  { key: 'preview', label: '授权预览' },
  { key: 'grants', label: '授权规则' },
  { key: 'exception', label: '紧急例外' },
  { key: 'requisition', label: '领料流转' },
  { key: 'downstream', label: '待办/装载/转组' },
] as const

const activeTab = ref<(typeof tabs)[number]['key']>('preview')
const identity = reactive({ unit: '城东养护所' })
const options = reactive<OptionSet>({ 工程: [], 材料类别: [], 领用单位: [], 权限: [] })
const message = ref('')
const messageOk = ref(true)

const grants = ref<Row[]>([])
const exceptions = ref<Row[]>([])
const requisitions = ref<Row[]>([])
const todos = ref<Row[]>([])
const loads = ref<Row[]>([])
const preview = ref<Preview | null>(null)

const previewForm = reactive({ project: 'PROJ-0001', section: '', category: '沥青类', unit: '城东养护所', quantity: 40 })
const grantForm = reactive({ 工程编号: 'PROJ-0001', 施工路段: '城东快速路', 材料类别: '沥青类', 领用单位: '', 权限: '领用', 授权额度: 100, 到期日期: '', 备注: '' })
const excForm = reactive({ 工程编号: '', 施工路段: '', 材料类别: '应急抢险类', 申请单位: '', 申请数量: 10, 事由: '' })
const reqForm = reactive({ 工程编号: 'PROJ-0001', 材料编号: 'MATE-0001', 领用单位: '城东养护所', 领用人: '', 申请数量: 10, 车牌号: '', 用途: '' })
const transferForm = reactive({ id: 1, new_owner: '' })

const pendingExceptionCount = computed(() => exceptions.value.filter((e) => e.状态 === '待指挥审批').length)
const pendingTodoCount = computed(() => todos.value.filter((t) => t.状态 === '待处理').length)
const enrichedTabs = computed(() =>
  tabs.map((t) => ({ ...t, badge: t.key === 'exception' ? pendingExceptionCount.value : t.key === 'downstream' ? pendingTodoCount.value : 0 })),
)

function flash(text: string, ok = true) {
  message.value = text
  messageOk.value = ok
}

function projectOf(code: string) {
  return options.工程.find((p) => p.工程编号 === code)
}

function permClass(perm: string) {
  if (perm === '领用') return 'perm-issue'
  if (perm === '只读') return 'perm-read'
  return 'perm-none'
}
function conclusionClass(c: string) {
  if (c === '放行') return 'perm-issue'
  if (c === '预占待发') return 'tag warn'
  return 'perm-none'
}
function exceptionClass(s: string) {
  if (s === '已批准') return 'perm-issue'
  if (s === '已驳回') return 'perm-none'
  return 'tag warn'
}

async function getJson(path: string) {
  const res = await request(path)
  if (!res.ok) throw new Error(`接口返回 ${res.status}`)
  return await res.json()
}
async function postJson(path: string, body: unknown) {
  const res = await request(path, { method: 'POST', body: JSON.stringify(body) })
  return await res.json()
}

async function loadOptions() {
  const data = await getJson(`${BASE}/options`)
  options.工程 = data.工程
  options.材料类别 = data.材料类别
  options.领用单位 = data.领用单位
  options.权限 = data.权限
  if (!options.领用单位.includes(identity.unit)) identity.unit = options.领用单位[0] ?? ''
  ;[grantForm, excForm, reqForm].forEach((f) => {
    if ('领用单位' in f && !f.领用单位) f.领用单位 = identity.unit
    if ('申请单位' in f && !(f as any).申请单位) (f as any).申请单位 = identity.unit
  })
}

async function loadGrants() {
  grants.value = (await getJson(`${BASE}/grants`)).items
}
async function loadExceptions() {
  exceptions.value = (await getJson(`${BASE}/exceptions`)).items
}
async function loadRequisitions() {
  requisitions.value = (await getJson(`${BASE}/requisitions`)).items
}
async function loadTodos() {
  todos.value = (await getJson(`${BASE}/todos`)).items
}
async function loadLoads() {
  loads.value = (await getJson(`${BASE}/loads`)).items
}
async function reloadAll() {
  await Promise.all([loadGrants(), loadExceptions(), loadRequisitions(), loadTodos(), loadLoads()])
  reqForm.领用单位 = identity.unit
}

function onPreviewProjectChange() {
  const p = projectOf(previewForm.project)
  previewForm.section = p?.施工路段 ?? ''
}
function syncGrantSection() {
  grantForm.施工路段 = projectOf(grantForm.工程编号)?.施工路段 ?? ''
}
function syncExcSection() {
  excForm.施工路段 = projectOf(excForm.工程编号)?.施工路段 ?? ''
}
function syncReqSection() {
  reqForm.领用单位 = identity.unit
}

async function runPreview() {
  const params = new URLSearchParams({
    project: previewForm.project,
    section: previewForm.section,
    category: previewForm.category,
    unit: previewForm.unit,
    quantity: String(previewForm.quantity || 0),
  })
  const data = await getJson(`${BASE}/preview?${params}`)
  if (data.ok === false) {
    preview.value = null
    flash(data.message, false)
    return
  }
  preview.value = data
}

async function submitGrant() {
  const res = await postJson(`${BASE}/grants`, {
    values: { ...grantForm, 授权人: identity.unit },
  })
  flash(res.message, res.ok)
  await loadGrants()
}
async function toggleGrantState(g: Row) {
  const next = g.状态 === '生效' ? '停用' : '生效'
  const res = await postJson(`${BASE}/grants/${g.id}/state`, { values: { 状态: next } })
  flash(res.message, res.ok)
  await loadGrants()
}

async function submitException() {
  if (!excForm.申请单位) excForm.申请单位 = identity.unit
  const res = await postJson(`${BASE}/exceptions`, { values: { ...excForm } })
  flash(res.message, res.ok)
  await loadExceptions()
}
async function approveExc(e: Row, approved: boolean) {
  const res = await postJson(`${BASE}/exceptions/${e.id}/approval`, {
    approver: '王指挥',
    approver_title: approved ? '现场总指挥' : '材料员',
    opinion: approved ? '同意临时领用，仅限本次抢险' : '不同意，走常规授权',
    approved,
  })
  flash(res.message, res.ok)
  await loadExceptions()
}

async function submitRequisition() {
  const res = await postJson(`${BASE}/requisitions`, { values: { ...reqForm } })
  flash(res.message, res.ok)
  await Promise.all([loadRequisitions(), loadGrants(), loadTodos(), loadLoads()])
}
async function issue(r: Row) {
  const res = await postJson(`${BASE}/requisitions/${r.id}/issue`, { values: { 车牌号: r.车牌号 || '' } })
  flash(res.message, res.ok)
  await Promise.all([loadRequisitions(), loadGrants(), loadTodos(), loadLoads()])
}

async function transferProject() {
  if (!transferForm.id || !transferForm.new_owner.trim()) {
    flash('请选择工程并填写新归属单位', false)
    return
  }
  const res = await postJson(`${BASE}/projects/${transferForm.id}/transfer`, {
    new_owner: transferForm.new_owner,
    operator: identity.unit,
  })
  flash(`${res.message}（历史领用单 ${res.entry?.冻结历史领用单数 ?? 0} 单保持原值）`, res.ok)
  transferForm.new_owner = ''
  await Promise.all([loadOptions(), reloadAll()])
}

onMounted(async () => {
  try {
    await loadOptions()
    await reloadAll()
    syncGrantSection()
    await runPreview()
  } catch (error) {
    flash(error instanceof Error ? error.message : '授权柜数据加载失败', false)
  }
})
</script>

<style scoped>
.grant-page .identity { align-items: flex-end; }
.tab-bar { display: flex; gap: 4px; border-bottom: 1px solid var(--border); margin-bottom: 12px; flex-wrap: wrap; }
.tab-btn { position: relative; border: none; background: none; padding: 8px 14px; cursor: pointer; font-size: 14px; color: var(--muted); border-bottom: 2px solid transparent; }
.tab-btn.active { color: var(--brand); border-bottom-color: var(--brand); font-weight: 600; }
.tab-badge { font-style: normal; background: #b42318; color: #fff; border-radius: 10px; font-size: 11px; padding: 0 6px; margin-left: 4px; }
.block-title { font-size: 14px; margin: 16px 0 8px; }
.decision-box { background: #fff; border: 1px solid var(--border); border-left: 4px solid var(--brand); border-radius: 8px; padding: 10px 12px; font-size: 13px; display: grid; gap: 4px; margin-bottom: 12px; }
.feasible { margin-top: 4px; font-weight: 600; color: #b42318; }
.feasible.ok { color: #067647; }
.grid-form { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 8px; }
.grid-form label { display: flex; flex-direction: column; font-size: 12px; color: var(--muted); gap: 4px; }
.grid-form label.span-2 { grid-column: span 2; }
.grid-form input, .grid-form select { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.form-actions { display: flex; justify-content: flex-end; }
.perm-issue { color: #067647; font-weight: 600; }
.perm-read { color: #b54708; font-weight: 600; }
.perm-none { color: #b42318; font-weight: 600; }
.tag { border-radius: 10px; padding: 1px 8px; font-size: 12px; }
.tag.warn { background: #fef3c7; color: #b45309; }
.tag.ok { background: #dcfae6; color: #067647; }
.tag.muted { background: #f1f5f9; color: var(--muted); }
.num-ok { color: #067647; }
.num-bad { color: #b42318; }
.rejected { background: #fff5f5; }
.reject-reason { color: #b42318; font-size: 12px; margin-top: 2px; }
.link.danger { color: #b42318; }
.muted-text { color: var(--muted); }
.hint { color: var(--muted); font-size: 12px; margin: 6px 0; }
.ok-text { color: #067647; }
.ellipsis { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
