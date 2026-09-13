export const OPENAI_PRICE_SOURCE = "https://developers.openai.com/api/docs/pricing";
type PriceDraft = {inputPrice: string; outputPrice: string; cacheReadPrice: string; cacheWritePrice: string};
const prices: Record<string, PriceDraft> = {
  "gpt-6-astra": {inputPrice:"10", outputPrice:"50", cacheReadPrice:"1", cacheWritePrice:"12.5"},
  "gpt-5.6-sol": {inputPrice:"4", outputPrice:"20", cacheReadPrice:"0.4", cacheWritePrice:"5"},
  "gpt-5.6-terra": {inputPrice:"2", outputPrice:"12", cacheReadPrice:"0.2", cacheWritePrice:"2.5"},
  "gpt-5.6-luna": {inputPrice:"0.2", outputPrice:"1.2", cacheReadPrice:"0.02", cacheWritePrice:"0.25"},
};
export default function ModelPriceReference({model, onApply, disabled = false}: {
  model: string; onApply: (prices: PriceDraft) => void; disabled?: boolean;
}) {
  const price = prices[model.trim()];
  return <div className="price-reference">
    <div><strong>模型费用参考</strong><a href={OPENAI_PRICE_SOURCE} target="_blank" rel="noreferrer">OpenAI 官方报价 · 2026-09-13 ↗</a></div>
    {price ? <><p>{model} · 输入 ${price.inputPrice} / 输出 ${price.outputPrice} / 缓存读取 ${price.cacheReadPrice} / 缓存写入 ${price.cacheWritePrice}（每百万 Token）。</p><button type="button" disabled={disabled} onClick={() => onApply({...price})}>填入此模型参考价格</button></> : <p>这个模型未收录参考价，请填写服务商给出的单价。</p>}
    <small>参考价适用于标准处理、短上下文。中转站、长上下文和加速档位可能采用不同价格；可按实际账单修改，保存后用于新请求估算。</small>
  </div>;
}
