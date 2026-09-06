"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { Search, Plus, Pencil, Trash2, Database, X, Loader2, FileUp } from "lucide-react";

type KnowledgeItem = {
  id: number;
  product_id: string;
  name: string;
  description: string;
  selling_points: string[];
  specs: string[];
  faq: string[];
};

type FormState = {
  product_id: string;
  name: string;
  description: string;
  selling_points: string;
  specs: string;
  faq: string;
};

const emptyForm: FormState = {
  product_id: "",
  name: "",
  description: "",
  selling_points: "",
  specs: "",
  faq: "",
};

export function KnowledgePanel() {
  const [items, setItems] = useState<KnowledgeItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<KnowledgeItem | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<FormState>(emptyForm);
  const [saving, setSaving] = useState(false);
  const [importing, setImporting] = useState(false);
  const [importingDoc, setImportingDoc] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const docFileInputRef = useRef<HTMLInputElement>(null);

  const loadItems = useCallback(async (q?: string) => {
    setLoading(true);
    try {
      const url = q
        ? `/api/knowledge/search?q=${encodeURIComponent(q)}`
        : "/api/knowledge";
      const res = await fetch(url);
      const data = await res.json();
      setItems(data.items || []);
    } catch (err) {
      console.error("加载知识库失败", err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadItems();
  }, [loadItems]);

  const handleSearch = () => {
    loadItems(query.trim() || undefined);
  };

  const openCreate = () => {
    setEditing(null);
    setForm(emptyForm);
    setShowForm(true);
  };

  const openEdit = (item: KnowledgeItem) => {
    setEditing(item);
    setForm({
      product_id: item.product_id,
      name: item.name,
      description: item.description,
      selling_points: item.selling_points.join("\n"),
      specs: item.specs.join("\n"),
      faq: item.faq.join("\n"),
    });
    setShowForm(true);
  };

  const handleSave = async () => {
    if (!form.name.trim()) {
      alert("商品名称不能为空");
      return;
    }
    setSaving(true);
    try {
      const body = {
        product_id: form.product_id.trim(),
        name: form.name.trim(),
        description: form.description.trim(),
        selling_points: form.selling_points.split("\n").filter((s) => s.trim()),
        specs: form.specs.split("\n").filter((s) => s.trim()),
        faq: form.faq.split("\n").filter((s) => s.trim()),
      };
      const url = editing ? `/api/knowledge/${editing.id}` : "/api/knowledge";
      const method = editing ? "PUT" : "POST";
      const res = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error("保存失败");
      setShowForm(false);
      loadItems();
    } catch (err) {
      alert("保存失败，请重试");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (item: KnowledgeItem) => {
    if (!confirm(`确定删除「${item.name}」的知识吗？`)) return;
    try {
      await fetch(`/api/knowledge/${item.id}`, { method: "DELETE" });
      loadItems();
    } catch (err) {
      alert("删除失败");
    }
  };

  const handleImport = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setImporting(true);
    try {
      const formData = new FormData();
      formData.append("file", file);
      const res = await fetch("/api/knowledge/import", {
        method: "POST",
        body: formData,
      });
      const data = await res.json();
      if (data.error) {
        alert(data.error);
      } else {
        const errMsg = data.errors?.length
          ? `\n跳过 ${data.errors.length} 行：\n${data.errors.slice(0, 5).join("\n")}`
          : "";
        alert(`导入完成：成功 ${data.success} 条，失败 ${data.failed} 条${errMsg}`);
        loadItems();
      }
    } catch (err) {
      alert("导入失败，请检查文件格式");
    } finally {
      setImporting(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const handleImportDoc = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setImportingDoc(true);
    try {
      const formData = new FormData();
      formData.append("file", file);
      const res = await fetch("/api/knowledge/import-doc", {
        method: "POST",
        body: formData,
      });
      const data = await res.json();
      if (data.error) {
        alert(data.error);
      } else {
        const errMsg = data.errors?.length
          ? `\n跳过 ${data.errors.length} 条：\n${data.errors.slice(0, 5).join("\n")}`
          : "";
        alert(`文档导入完成：成功提取 ${data.success} 条商品知识${errMsg}`);
        loadItems();
      }
    } catch (err) {
      alert("文档导入失败，请重试");
    } finally {
      setImportingDoc(false);
      if (docFileInputRef.current) docFileInputRef.current.value = "";
    }
  };

  return (
    <div className="flex flex-col h-full bg-white rounded-xl shadow-sm border border-gray-200">
      {/* 标题栏 */}
      <div className="bg-orange-500 text-white h-12 px-4 flex items-center rounded-t-xl">
        <Database className="h-5 w-5 mr-2" />
        <h2 className="font-semibold text-sm">商品知识库管理</h2>
        <span className="ml-auto text-xs font-light opacity-80">
          运营视角 · 共 {items.length} 条
        </span>
      </div>

      {/* 工具栏 */}
      <div className="flex gap-2 p-3 border-b border-gray-100">
        <div className="flex-1 flex gap-2">
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSearch()}
            placeholder="搜索商品知识..."
            className="flex-1 px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-orange-200"
          />
          <button
            onClick={handleSearch}
            className="px-3 py-2 bg-gray-100 text-gray-600 rounded-lg hover:bg-gray-200"
          >
            <Search className="h-4 w-4" />
          </button>
        </div>
        <button
          onClick={() => fileInputRef.current?.click()}
          disabled={importing}
          className="px-3 py-2 bg-white border border-gray-200 text-gray-600 rounded-lg hover:bg-gray-50 flex items-center gap-1 text-sm disabled:opacity-50"
        >
          {importing ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <FileUp className="h-4 w-4" />
          )}
          导入CSV
        </button>
        <input
          ref={fileInputRef}
          type="file"
          accept=".csv"
          onChange={handleImport}
          className="hidden"
        />
        <button
          onClick={() => docFileInputRef.current?.click()}
          disabled={importingDoc}
          className="px-3 py-2 bg-white border border-gray-200 text-gray-600 rounded-lg hover:bg-gray-50 flex items-center gap-1 text-sm disabled:opacity-50"
        >
          {importingDoc ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <FileUp className="h-4 w-4" />
          )}
          导入文档
        </button>
        <input
          ref={docFileInputRef}
          type="file"
          accept=".pdf,.docx,.txt,.md"
          onChange={handleImportDoc}
          className="hidden"
        />
        <button
          onClick={openCreate}
          className="px-3 py-2 bg-orange-500 text-white rounded-lg hover:bg-orange-600 flex items-center gap-1 text-sm"
        >
          <Plus className="h-4 w-4" />
          新增知识
        </button>
      </div>

      {/* 列表 */}
      <div className="flex-1 overflow-y-auto p-3 space-y-3">
        {loading ? (
          <div className="flex justify-center py-10 text-gray-400">
            <Loader2 className="h-5 w-5 animate-spin" />
          </div>
        ) : items.length === 0 ? (
          <div className="text-center py-10 text-gray-400 text-sm">
            暂无商品知识，点击"新增知识"添加
          </div>
        ) : (
          items.map((item) => (
            <div
              key={item.id}
              className="border border-gray-200 rounded-lg p-3 hover:border-orange-200 transition-colors"
            >
              <div className="flex items-start justify-between">
                <div className="flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-sm text-gray-800">
                      {item.name}
                    </span>
                    {item.product_id && (
                      <span className="text-xs font-mono text-gray-400">
                        {item.product_id}
                      </span>
                    )}
                  </div>
                  {item.description && (
                    <p className="text-xs text-gray-500 mt-1">{item.description}</p>
                  )}
                  {item.selling_points.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {item.selling_points.slice(0, 3).map((s, i) => (
                        <span
                          key={i}
                          className="text-[11px] bg-orange-50 text-orange-600 px-1.5 py-0.5 rounded"
                        >
                          {s}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
                <div className="flex gap-1 ml-2">
                  <button
                    onClick={() => openEdit(item)}
                    className="p-1.5 text-gray-400 hover:text-orange-500 hover:bg-orange-50 rounded"
                  >
                    <Pencil className="h-4 w-4" />
                  </button>
                  <button
                    onClick={() => handleDelete(item)}
                    className="p-1.5 text-gray-400 hover:text-red-500 hover:bg-red-50 rounded"
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
              </div>
            </div>
          ))
        )}
      </div>

      {/* 新增/编辑弹窗 */}
      {showForm && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl shadow-xl w-[520px] max-h-[85vh] flex flex-col">
            <div className="px-4 py-3 border-b border-gray-100 flex items-center justify-between">
              <h3 className="font-semibold text-sm">
                {editing ? "编辑商品知识" : "新增商品知识"}
              </h3>
              <button onClick={() => setShowForm(false)} className="text-gray-400 hover:text-gray-600">
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto p-4 space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs text-gray-500 mb-1 block">商品名称 *</label>
                  <input
                    value={form.name}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                    className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-orange-200"
                    placeholder="如：良品铺子坚果大礼包"
                  />
                </div>
                <div>
                  <label className="text-xs text-gray-500 mb-1 block">商品ID</label>
                  <input
                    value={form.product_id}
                    onChange={(e) => setForm({ ...form, product_id: e.target.value })}
                    className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-orange-200"
                    placeholder="如：P1001"
                  />
                </div>
              </div>
              <div>
                <label className="text-xs text-gray-500 mb-1 block">商品介绍</label>
                <textarea
                  value={form.description}
                  onChange={(e) => setForm({ ...form, description: e.target.value })}
                  rows={2}
                  className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-orange-200"
                  placeholder="商品的详细介绍"
                />
              </div>
              <div>
                <label className="text-xs text-gray-500 mb-1 block">卖点（每行一个）</label>
                <textarea
                  value={form.selling_points}
                  onChange={(e) => setForm({ ...form, selling_points: e.target.value })}
                  rows={3}
                  className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-orange-200"
                  placeholder={"卖点1\n卖点2"}
                />
              </div>
              <div>
                <label className="text-xs text-gray-500 mb-1 block">规格（每行一个）</label>
                <textarea
                  value={form.specs}
                  onChange={(e) => setForm({ ...form, specs: e.target.value })}
                  rows={2}
                  className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-orange-200"
                  placeholder={"规格1\n规格2"}
                />
              </div>
              <div>
                <label className="text-xs text-gray-500 mb-1 block">常见问题（每行一个）</label>
                <textarea
                  value={form.faq}
                  onChange={(e) => setForm({ ...form, faq: e.target.value })}
                  rows={3}
                  className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-orange-200"
                  placeholder={"保质期多久：180天\n是否含糖：无添加糖"}
                />
              </div>
            </div>
            <div className="px-4 py-3 border-t border-gray-100 flex justify-end gap-2">
              <button
                onClick={() => setShowForm(false)}
                className="px-4 py-2 text-sm text-gray-600 hover:bg-gray-100 rounded-lg"
              >
                取消
              </button>
              <button
                onClick={handleSave}
                disabled={saving}
                className="px-4 py-2 text-sm bg-orange-500 text-white rounded-lg hover:bg-orange-600 disabled:opacity-50"
              >
                {saving ? "保存中..." : "保存"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
