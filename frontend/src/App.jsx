import { useState, useEffect } from 'react';
import Sidebar from './components/Sidebar';
import ChatInterface from './components/ChatInterface';
import { api } from './api';
import './App.css';

function App() {
  const [conversations, setConversations] = useState([]);
  const [currentConversationId, setCurrentConversationId] = useState(null);
  const [currentConversation, setCurrentConversation] = useState(null);
  const [isLoading, setIsLoading] = useState(false);

  // Load conversations on mount
  useEffect(() => {
    loadConversations();
  }, []);

  // Load conversation details when selected
  useEffect(() => {
    if (currentConversationId) {
      loadConversation(currentConversationId);
    }
  }, [currentConversationId]);

  const loadConversations = async () => {
    try {
      const convs = await api.listConversations();
      setConversations(convs);
    } catch (error) {
      console.error('Failed to load conversations:', error);
    }
  };

  const loadConversation = async (id) => {
    try {
      const conv = await api.getConversation(id);
      setCurrentConversation(conv);
    } catch (error) {
      console.error('Failed to load conversation:', error);
    }
  };

  const handleNewConversation = async () => {
    try {
      const newConv = await api.createConversation();
      setConversations([
        { id: newConv.id, created_at: newConv.created_at, message_count: 0 },
        ...conversations,
      ]);
      setCurrentConversationId(newConv.id);
    } catch (error) {
      console.error('Failed to create conversation:', error);
    }
  };

  const handleSelectConversation = (id) => {
    setCurrentConversationId(id);
  };

  const handleStreamEvent = (conversationId, eventType, event) => {
    switch (eventType) {
      case 'stage1_start':
        setCurrentConversation((prev) => {
          const messages = [...prev.messages];
          const lastMsg = messages[messages.length - 1];
          lastMsg.loading.stage1 = true;
          return { ...prev, messages };
        });
        break;

      case 'stage1_complete':
        setCurrentConversation((prev) => {
          const messages = [...prev.messages];
          const lastMsg = messages[messages.length - 1];
          lastMsg.stage1 = event.data;
          lastMsg.loading.stage1 = false;
          return { ...prev, messages };
        });
        break;

      case 'stage2_start':
        setCurrentConversation((prev) => {
          const messages = [...prev.messages];
          const lastMsg = messages[messages.length - 1];
          lastMsg.loading.stage2 = true;
          return { ...prev, messages };
        });
        break;

      case 'stage2_complete':
        setCurrentConversation((prev) => {
          const messages = [...prev.messages];
          const lastMsg = messages[messages.length - 1];
          lastMsg.stage2 = event.data;
          lastMsg.metadata = event.metadata;
          lastMsg.loading.stage2 = false;
          return { ...prev, messages };
        });
        break;

      case 'stage3_start':
        setCurrentConversation((prev) => {
          const messages = [...prev.messages];
          const lastMsg = messages[messages.length - 1];
          lastMsg.loading.stage3 = true;
          return { ...prev, messages };
        });
        break;

      case 'stage3_complete':
        setCurrentConversation((prev) => {
          const messages = [...prev.messages];
          const lastMsg = messages[messages.length - 1];
          lastMsg.stage3 = event.data;
          lastMsg.loading.stage3 = false;
          return { ...prev, messages };
        });
        break;

      case 'title_complete':
        loadConversations();
        break;

      case 'complete':
        loadConversations();
        loadConversation(conversationId);
        setIsLoading(false);
        break;

      case 'error':
        console.error('Stream error:', event.message);
        loadConversations();
        loadConversation(conversationId);
        setIsLoading(false);
        break;

      default:
        console.log('Unknown event type:', eventType);
    }
  };

  const runStreamRequest = async ({ conversationId, requestFn, appendUserMessage, content }) => {
    if (!conversationId) return;

    setIsLoading(true);
    try {
      if (appendUserMessage) {
        const userMessage = { role: 'user', content };
        setCurrentConversation((prev) => ({
          ...prev,
          messages: [...prev.messages, userMessage],
        }));
      }

      const assistantMessage = {
        role: 'assistant',
        stage1: null,
        stage2: null,
        stage3: null,
        metadata: null,
        loading: {
          stage1: false,
          stage2: false,
          stage3: false,
        },
      };

      setCurrentConversation((prev) => ({
        ...prev,
        messages: [...prev.messages, assistantMessage],
      }));

      await requestFn((eventType, event) => {
        handleStreamEvent(conversationId, eventType, event);
      });
    } catch (error) {
      console.error('Failed to run conversation request:', error);
      loadConversations();
      loadConversation(conversationId);
      setIsLoading(false);
    }
  };

  const handleSendMessage = async (content) => {
    if (!currentConversationId) return;

    await runStreamRequest({
      conversationId: currentConversationId,
      requestFn: (onEvent) => api.sendMessageStream(currentConversationId, content, onEvent),
      appendUserMessage: true,
      content,
    });
  };

  const handleRetryLastMessage = async () => {
    if (!currentConversationId) return;

    await runStreamRequest({
      conversationId: currentConversationId,
      requestFn: (onEvent) => api.retryMessageStream(currentConversationId, onEvent),
      appendUserMessage: false,
    });
  };

  const canRetryLastMessage =
    !!currentConversation &&
    currentConversation.messages.length > 0 &&
    currentConversation.messages[currentConversation.messages.length - 1].role === 'user' &&
    !isLoading;

  return (
    <div className="app">
      <Sidebar
        conversations={conversations}
        currentConversationId={currentConversationId}
        onSelectConversation={handleSelectConversation}
        onNewConversation={handleNewConversation}
      />
      <ChatInterface
        conversation={currentConversation}
        onSendMessage={handleSendMessage}
        onRetryLastMessage={handleRetryLastMessage}
        canRetryLastMessage={canRetryLastMessage}
        isLoading={isLoading}
      />
    </div>
  );
}

export default App;
