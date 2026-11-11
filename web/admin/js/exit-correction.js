// 出口异常纠正的纯数据逻辑：开放记录 -> correct-exit 请求体，便于 Node 单测。
export const ENTRY_UNITS = ['min', 'sec'];

export function entryValueFromRecord(record, unit = 'min') {
  const sec = Number(record?.entry_sim_sec);
  if (!Number.isFinite(sec)) return '';
  const value = unit === 'min' ? sec / 60 : sec;
  return String(Math.round(value * 100) / 100);
}

export function buildCorrectionPayload({
  recordId = null, plate = '', action = 'auto', unit = 'min', value = '', reason = '', requestId = '',
} = {}) {
  const payload = {
    plate: String(plate || '').trim().toUpperCase(),
    action: recordId ? 'auto' : (action || 'auto'),
  };
  if (recordId != null && String(recordId) !== '') payload.record_id = Number(recordId);
  const number = Number(value);
  if (String(value).trim() !== '' && Number.isFinite(number)) {
    if (unit === 'sec') payload.entry_sim_sec = number;
    else payload.entry_sim_min = number;
  }
  if (reason) payload.reason = String(reason);
  if (requestId) payload.request_id = String(requestId);
  return payload;
}

export function validateCorrection({
  recordId = null, plate = '', action = 'auto', unit = 'min', value = '', reason = '',
} = {}) {
  const plateText = String(plate || '').trim().toUpperCase();
  if (!plateText) return { message: '请输入正确车牌', field: 'plate' };
  if (!/^[\u4e00-\u9fa5][A-Z][A-Z0-9]{5,6}$/.test(plateText) || ![7, 8].includes(plateText.length)) {
    return { message: '车牌需为 7/8 位中文车牌', field: 'plate' };
  }
  const hasRecord = recordId != null && String(recordId) !== '';
  if (!hasRecord && !['free', 'void'].includes(action)) {
    return { message: '未选择开放记录时必须选择免费或作废', field: 'record' };
  }
  if (String(value).trim() !== '') {
    const number = Number(value);
    if (!Number.isFinite(number) || number < 0) {
      return { message: '入场时间必须为 ≥0 的数字', field: 'entry' };
    }
  }
  if (!hasRecord && !String(reason || '').trim()) {
    return { message: '免费/作废必须填写原因', field: 'reason' };
  }
  return null;
}

export function settlementSummary(settlement = {}) {
  const action = settlement.action || 'auto';
  const amount = settlement.amount_paid;
  if (action === 'void') return { label: '已作废', detail: '出口已开' };
  if (action === 'free') return { label: '已免费放行', detail: '出口已开' };
  if (amount != null) return { label: `已收费 ¥${Number(amount).toFixed(2)}`, detail: '记录已 out，出口已开' };
  return { label: '已结算', detail: '记录已 out，出口已开' };
}
