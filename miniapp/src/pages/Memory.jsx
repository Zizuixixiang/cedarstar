/**
 * 记忆管理页面 - 完整实现
 * 管理 AI 助手的记忆卡片和长期记忆库
 */

import { useState, useEffect, useRef, useCallback } from 'react';
import './../styles/memory.css';

// API 基础 URL
const API_BASE_URL = 'http://localhost:8000';

// 维度映射
const DIMENSION_MAP = {
  preferences: '偏好',
  interaction_patterns: '相处模式',
  current_status: '近况',
  goals: '目标',
  relationships: '关系',
  key_events: '重要事件',
  rules: '规则'
};

/**
 * Toast 提示组件
 */
function Toast({ message, type = 'info', onClose }) {
  useEffect(() => {
    const timer = setTimeout(() => {
      onClose();
    }, 2000);
    
    return () => clearTimeout(timer);
  }, [onClose]);
  
  return (
    <div className={`toast ${type}`}>
      {type === 'success' && '✓'}
      {type === 'error' && '✗'}
      {type === 'info' && 'ℹ️'}
      <span>{message}</span>
    </div>
  );
}

/**
 * 编辑弹窗组件
 */
function EditModal({ dimension, content, onClose, onSave }) {
  const [editContent, setEditContent] = useState(content || '');
  const [showConfirm, setShowConfirm] = useState(false);
  
  const handleSave = () => {
    setShowConfirm(true);
  };
  
  const confirmSave = () => {
    onSave(dimension, editContent);
    setShowConfirm(false);
  };
  
  if (showConfirm) {
    return (
      <div className="modal-overlay">
        <div className="modal-container confirm-modal">
          <div className="modal-title">确认更新</div>
          <div className="confirm-message">
            确认更新<span style={{ color: 'var(--accent)', fontWeight: '500' }}> {DIMENSION_MAP[dimension]} </span>
            的记忆卡片吗？
          </div>
          <div className="confirm-warning">此操作将覆盖原有的内容。</div>
          <div className="modal-actions">
            <button className="modal-button cancel" onClick={() => setShowConfirm(false)}>
              取消
            </button>
            <button className="modal-button confirm" onClick={confirmSave}>
              确认更新
            </button>
          </div>
        </div>
      </div>
    );
  }
  
  return (
    <div className="modal-overlay">
      <div className="modal-container">
        <div className="modal-title">编辑记忆卡片</div>
        <div className="modal-section">
          <div className="modal-label">维度：{DIMENSION_MAP[dimension]}</div>
          {content && (
            <>
              <div className="modal-label">当前内容：</div>
              <div className="current-content">
                {content}
              </div>
            </>
          )}
        </div>
        <div className="modal-section">
          <div className="modal-label">编辑内容：</div>
          <textarea
            className="edit-textarea"
            value={editContent}
            onChange={(e) => setEditContent(e.target.value)}
            placeholder="在此编辑记忆内容..."
            autoFocus
          />
        </div>
        <div className="modal-actions">
          <button className="modal-button cancel" onClick={onClose}>
            取消
          </button>
          <button className="modal-button confirm" onClick={handleSave}>
            保存
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * 删除确认弹窗组件
 */
function DeleteConfirmModal({ dimension, onClose, onConfirm }) {
  return (
    <div className="modal-overlay">
      <div className="modal-container confirm-modal">
        <div className="modal-title">确认删除</div>
        <div className="confirm-message">
          此操作将清空<span style={{ color: '#E07070', fontWeight: '500' }}> {DIMENSION_MAP[dimension]} </span>
          维度的记忆内容
        </div>
        <div className="confirm-warning">删除后不可恢复，确认删除吗？</div>
        <div className="modal-actions">
          <button className="modal-button cancel" onClick={onClose}>
            取消
          </button>
          <button className="modal-button delete" onClick={onConfirm}>
            确认删除
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * 新增记忆弹窗组件
 */
function AddMemoryModal({ onClose, onSubmit }) {
  const [content, setContent] = useState('');
  const [submitting, setSubmitting] = useState(false);
  
  const handleSubmit = async () => {
    if (!content.trim() || submitting) {
      return;
    }
    setSubmitting(true);
    try {
      await onSubmit(content);
    } finally {
      setSubmitting(false);
    }
  };
  
  return (
    <div className="modal-overlay">
      <div className="modal-container">
        <div className="modal-title">新增长期记忆</div>
        <div className="modal-section">
          <div className="modal-label">记忆内容：</div>
          <textarea
            className="edit-textarea"
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder="输入要记录的长期记忆内容..."
            autoFocus
            disabled={submitting}
          />
        </div>
        <div className="modal-actions">
          <button className="modal-button cancel" onClick={onClose} disabled={submitting}>
            取消
          </button>
          <button className="modal-button confirm" onClick={handleSubmit} disabled={!content.trim() || submitting}>
            {submitting ? '提交中...' : '提交'}
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * 记忆卡片组件
 */
function MemoryCard({ dimension, content, updatedAt, onEdit, onDelete }) {
  const isEmpty = !content || content.trim() === '';
  const displayContent = isEmpty ? '暂无内容，点击编辑添加' : content;
  const displayTime = updatedAt ? new Date(updatedAt).toLocaleDateString('zh-CN') : '未记录';
  
  return (
    <div className={`memory-card ${isEmpty ? 'empty' : ''}`}>
      <div className="card-header">
        <div className="card-title">{DIMENSION_MAP[dimension]}</div>
        <div className="card-actions">
          <button className="action-button edit-button" onClick={() => onEdit(dimension)}>
            编辑
          </button>
          {!isEmpty && (
            <button className="action-button delete-card-button" onClick={() => onDelete(dimension)}>
              删除
            </button>
          )}
        </div>
      </div>
      <div className={`card-content ${isEmpty ? 'empty' : ''}`}>
        {displayContent}
      </div>
      <div className="card-footer">
        <div className="card-timestamp">更新: {displayTime}</div>
      </div>
    </div>
  );
}

/**
 * 长期记忆项组件
 */
function LongTermMemoryItem({ memory, onDelete }) {
  const [showConfirm, setShowConfirm] = useState(false);
  
  const handleDelete = () => {
    setShowConfirm(true);
  };
  
  const confirmDelete = () => {
    onDelete(memory.id);
    setShowConfirm(false);
  };
  
  if (showConfirm) {
    return (
      <div className="modal-overlay">
        <div className="modal-container confirm-modal">
          <div className="modal-title">确认删除</div>
          <div className="confirm-message">确认删除这条长期记忆吗？</div>
          <div className="confirm-warning">删除后不可恢复。</div>
          <div className="modal-actions">
            <button className="modal-button cancel" onClick={() => setShowConfirm(false)}>
              取消
            </button>
            <button className="modal-button delete" onClick={confirmDelete}>
              确认删除
            </button>
          </div>
        </div>
      </div>
    );
  }
  
  return (
    <div className="memory-item">
      <div className="memory-summary">{memory.content}</div>
      <div className="memory-meta">
        <span>归档: {new Date(memory.created_at).toLocaleDateString('zh-CN')}</span>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <span className="score-badge">★ {memory.score || '0'}分</span>
          <button className="delete-button" onClick={handleDelete}>
            删除
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * 骨架屏组件
 */
function SkeletonLoader() {
  return (
    <div className="memory-container">
      {/* 记忆卡片骨架屏 */}
      <div>
        <div className="section-title">
          <div className="skeleton-line" style={{ width: '120px' }}></div>
        </div>
        <div className="skeleton-card-grid">
          {[...Array(7)].map((_, i) => (
            <div key={i} className="skeleton-card">
              <div className="skeleton-line short"></div>
              <div className="skeleton-line medium"></div>
              <div className="skeleton-line" style={{ width: '40%' }}></div>
            </div>
          ))}
        </div>
      </div>
      
      {/* 长期记忆骨架屏 */}
      <div className="longterm-section">
        <div className="section-title">
          <div className="skeleton-line" style={{ width: '120px' }}></div>
          <div className="skeleton-line" style={{ width: '100px' }}></div>
        </div>
        <div className="longterm-header">
          <div className="skeleton-line" style={{ width: '100%' }}></div>
          <div className="skeleton-line" style={{ width: '80px' }}></div>
        </div>
        <div className="memory-list">
          {[...Array(3)].map((_, i) => (
            <div key={i} className="skeleton-card">
              <div className="skeleton-line medium"></div>
              <div className="skeleton-line short"></div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/**
 * 主 Memory 组件
 */
function Memory() {
  // 状态管理
  const [loading, setLoading] = useState(true);
  const [memoryCards, setMemoryCards] = useState({});
  const [longTermMemories, setLongTermMemories] = useState([]);
  const [toasts, setToasts] = useState([]);
  
  // 弹窗状态
  const [showEditModal, setShowEditModal] = useState(false);
  const [editingDimension, setEditingDimension] = useState(null);
  const [editingContent, setEditingContent] = useState('');
  
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [deletingDimension, setDeletingDimension] = useState(null);
  
  const [showAddModal, setShowAddModal] = useState(false);
  
  // 搜索和分页
  const [searchKeyword, setSearchKeyword] = useState('');
  const [currentPage, setCurrentPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const searchTimeoutRef = useRef(null);
  
  // 添加 Toast
  const addToast = useCallback((message, type = 'info') => {
    const id = Date.now();
    setToasts(prev => [...prev, { id, message, type }]);
  }, []);
  
  // 移除 Toast
  const removeToast = useCallback((id) => {
    setToasts(prev => prev.filter(toast => toast.id !== id));
  }, []);
  
  // 加载记忆卡片数据
  const loadMemoryCards = useCallback(async () => {
    try {
      const response = await fetch(`${API_BASE_URL}/api/memory/cards`);
      if (!response.ok) {
        throw new Error('获取记忆卡片失败');
      }
      const data = await response.json();
      
      if (data.success) {
        // 将卡片按维度分组
        const cardsByDimension = {};
        const dimensions = Object.keys(DIMENSION_MAP);
        
        // 初始化所有维度
        dimensions.forEach(dim => {
          cardsByDimension[dim] = {
            id: null,
            content: '',
            updated_at: null
          };
        });
        
        // 填充已有卡片
        if (data.data && Array.isArray(data.data)) {
          data.data.forEach(card => {
            if (card.dimension && DIMENSION_MAP[card.dimension]) {
              cardsByDimension[card.dimension] = {
                id: card.id || null,
                content: card.content || '',
                updated_at: card.updated_at || card.created_at
              };
            }
          });
        }
        
        setMemoryCards(cardsByDimension);
      }
    } catch (error) {
      console.error('加载记忆卡片失败:', error);
      // 初始化空卡片
      const emptyCards = {};
      Object.keys(DIMENSION_MAP).forEach(dim => {
        emptyCards[dim] = { id: null, content: '', updated_at: null };
      });
      setMemoryCards(emptyCards);
      addToast('加载记忆卡片失败：' + error.message, 'error');
    }
  }, [addToast]);
  
  // 加载长期记忆数据
  const loadLongTermMemories = useCallback(async (keyword = '', page = 1) => {
    try {
      const params = new URLSearchParams({
        keyword,
        page: page.toString(),
        page_size: '20'
      });
      
      const response = await fetch(`${API_BASE_URL}/api/memory/longterm?${params}`);
      if (!response.ok) {
        throw new Error('获取长期记忆失败');
      }
      const data = await response.json();
      
      if (data.success) {
        setLongTermMemories(data.data?.items || []);
        setTotalPages(data.data?.total_pages || 1);
        setCurrentPage(data.data?.current_page || 1);
      }
    } catch (error) {
      console.error('加载长期记忆失败:', error);
      setLongTermMemories([]);
      setTotalPages(1);
      setCurrentPage(1);
      addToast('加载长期记忆失败：' + error.message, 'error');
    }
  }, [addToast]);
  
  // 初始化加载数据
  useEffect(() => {
    const loadAllData = async () => {
      setLoading(true);
      await Promise.all([
        loadMemoryCards(),
        loadLongTermMemories()
      ]);
      setLoading(false);
    };
    
    loadAllData();
  }, [loadMemoryCards, loadLongTermMemories]);
  
  // 搜索防抖
  useEffect(() => {
    if (searchTimeoutRef.current) {
      clearTimeout(searchTimeoutRef.current);
    }
    
    searchTimeoutRef.current = setTimeout(() => {
      loadLongTermMemories(searchKeyword, 1);
    }, 500);
    
    return () => {
      if (searchTimeoutRef.current) {
        clearTimeout(searchTimeoutRef.current);
      }
    };
  }, [searchKeyword, loadLongTermMemories]);
  
  // 处理编辑记忆卡片
  const handleEditCard = (dimension) => {
    setEditingDimension(dimension);
    setEditingContent(memoryCards[dimension]?.content || '');
    setShowEditModal(true);
  };
  
  const handleSaveCard = async (dimension, content) => {
    try {
      const card = memoryCards[dimension];
      const cardId = card?.id;
      
      if (cardId) {
        // 更新现有卡片
        const response = await fetch(`${API_BASE_URL}/api/memory/cards/${cardId}`, {
          method: 'PUT',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            content,
            dimension
          })
        });
        
        if (!response.ok) {
          throw new Error('更新卡片失败');
        }
        
        addToast('记忆卡片更新成功', 'success');
      } else {
        // 创建新卡片 - 使用POST /api/memory/cards
        const response = await fetch(`${API_BASE_URL}/api/memory/cards`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            user_id: 'default_user',
            character_id: 'default_character',
            dimension: dimension,
            content: content
          })
        });
        
        if (!response.ok) {
          throw new Error('创建卡片失败');
        }
        
        const data = await response.json();
        if (data.success) {
          addToast('记忆卡片创建成功', 'success');
          // 回写服务器分配的 card_id，避免下次编辑时重复创建
          const newCardId = data.data?.card_id || null;
          setMemoryCards(prev => ({
            ...prev,
            [dimension]: {
              id: newCardId,
              content,
              updated_at: new Date().toISOString()
            }
          }));
          setShowEditModal(false);
          return;
        } else {
          throw new Error(data.message || '创建卡片失败');
        }
      }
      
      // 更新现有卡片的本地状态
      setMemoryCards(prev => ({
        ...prev,
        [dimension]: {
          ...prev[dimension],
          content,
          updated_at: new Date().toISOString()
        }
      }));
      
      setShowEditModal(false);
    } catch (error) {
      console.error('保存记忆卡片失败:', error);
      addToast('操作失败，请重试', 'error');
    }
  };
  
  // 处理删除记忆卡片
  const handleDeleteCard = (dimension) => {
    setDeletingDimension(dimension);
    setShowDeleteModal(true);
  };
  
  const confirmDeleteCard = async () => {
    try {
      const card = memoryCards[deletingDimension];
      const cardId = card?.id;
      
      if (cardId) {
        // 调用删除API
        const response = await fetch(`${API_BASE_URL}/api/memory/cards/${cardId}`, {
          method: 'DELETE'
        });
        
        if (!response.ok) {
          const errorData = await response.json();
          throw new Error(errorData.message || '删除卡片失败');
        }
        
        const data = await response.json();
        if (data.success) {
          addToast('记忆卡片已清空', 'success');
        } else {
          throw new Error(data.message || '删除卡片失败');
        }
      } else {
        // 没有cardId，说明卡片不存在或为空，直接更新本地状态
        addToast('记忆卡片已清空', 'success');
      }
      
      // 更新本地状态
      setMemoryCards(prev => ({
        ...prev,
        [deletingDimension]: {
          ...prev[deletingDimension],
          id: null,
          content: '',
          updated_at: null
        }
      }));
      
      setShowDeleteModal(false);
    } catch (error) {
      console.error('删除记忆卡片失败:', error);
      addToast(`操作失败：${error.message}`, 'error');
    }
  };
  
  // 处理新增长期记忆
  const handleAddMemory = async (content) => {
    try {
      // 调用新增API
      const response = await fetch(`${API_BASE_URL}/api/memory/longterm`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          content
        })
      });
      
      if (!response.ok) {
        throw new Error('新增记忆失败');
      }
      
      addToast('长期记忆添加成功', 'success');
      setShowAddModal(false);
      
      // 重新加载数据
      loadLongTermMemories(searchKeyword, currentPage);
    } catch (error) {
      console.error('新增长期记忆失败:', error);
      addToast('操作失败，请重试', 'error');
    }
  };
  
  // 处理删除长期记忆
  const handleDeleteMemory = async (memoryId) => {
    try {
      // 调用删除API
      const response = await fetch(`${API_BASE_URL}/api/memory/longterm/${memoryId}`, {
        method: 'DELETE'
      });
      
      if (!response.ok) {
        throw new Error('删除记忆失败');
      }
      
      addToast('长期记忆删除成功', 'success');
      
      // 重新加载数据
      loadLongTermMemories(searchKeyword, currentPage);
    } catch (error) {
      console.error('删除长期记忆失败:', error);
      addToast('操作失败，请重试', 'error');
    }
  };
  
  // 处理分页
  const handlePrevPage = () => {
    if (currentPage > 1) {
      const newPage = currentPage - 1;
      setCurrentPage(newPage);
      loadLongTermMemories(searchKeyword, newPage);
    }
  };
  
  const handleNextPage = () => {
    if (currentPage < totalPages) {
      const newPage = currentPage + 1;
      setCurrentPage(newPage);
      loadLongTermMemories(searchKeyword, newPage);
    }
  };
  
  if (loading) {
    return <SkeletonLoader />;
  }
  
  return (
    <div className="memory-container">
      {/* Toast 提示容器 */}
      <div className="toast-container">
        {toasts.map(toast => (
          <Toast
            key={toast.id}
            message={toast.message}
            type={toast.type}
            onClose={() => removeToast(toast.id)}
          />
        ))}
      </div>
      
      {/* 编辑弹窗 */}
      {showEditModal && (
        <EditModal
          dimension={editingDimension}
          content={editingContent}
          onClose={() => setShowEditModal(false)}
          onSave={handleSaveCard}
        />
      )}
      
      {/* 删除确认弹窗 */}
      {showDeleteModal && (
        <DeleteConfirmModal
          dimension={deletingDimension}
          onClose={() => setShowDeleteModal(false)}
          onConfirm={confirmDeleteCard}
        />
      )}
      
      {/* 新增记忆弹窗 */}
      {showAddModal && (
        <AddMemoryModal
          onClose={() => setShowAddModal(false)}
          onSubmit={handleAddMemory}
        />
      )}
      
      {/* 记忆卡片区块 */}
      <div>
        <div className="section-title">
          <span>📓 记忆卡片</span>
        </div>
        <div className="memory-cards-grid">
          {Object.keys(DIMENSION_MAP).map(dimension => (
            <MemoryCard
              key={dimension}
              dimension={dimension}
              content={memoryCards[dimension]?.content}
              updatedAt={memoryCards[dimension]?.updated_at}
              onEdit={handleEditCard}
              onDelete={handleDeleteCard}
            />
          ))}
        </div>
      </div>
      
      {/* 长期记忆区块 */}
      <div className="longterm-section">
        <div className="section-title">
          <span>📚 长期记忆库</span>
          <button className="add-button" onClick={() => setShowAddModal(true)}>
            + 手动新增
          </button>
        </div>
        
        <div className="longterm-header">
          <input
            type="text"
            className="search-input"
            placeholder="搜索长期记忆..."
            value={searchKeyword}
            onChange={(e) => setSearchKeyword(e.target.value)}
          />
        </div>
        
        <div className="memory-list">
          {longTermMemories.length === 0 ? (
            <div className="empty-state">
              <div className="empty-state-icon">📝</div>
              <div className="empty-state-text">暂无长期记忆记录</div>
            </div>
          ) : (
            longTermMemories.map(memory => (
              <LongTermMemoryItem
                key={memory.id}
                memory={memory}
                onDelete={handleDeleteMemory}
              />
            ))
          )}
        </div>
        
        {/* 分页控件 */}
        {longTermMemories.length > 0 && (
          <div className="pagination">
            <button
              className="pagination-button"
              onClick={handlePrevPage}
              disabled={currentPage <= 1}
            >
              上一页
            </button>
            <span className="pagination-info">
              第 {currentPage} 页 / 共 {totalPages} 页
            </span>
            <button
              className="pagination-button"
              onClick={handleNextPage}
              disabled={currentPage >= totalPages}
            >
              下一页
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export default Memory;