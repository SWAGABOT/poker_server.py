from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import json
import random
import asyncio
from typing import Dict, List, Optional
from datetime import datetime
import sqlite3

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://swagabot.github.io"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ======================================
# ==== БАЗА ДАННЫХ =====================
# ======================================
conn = sqlite3.connect('poker.db', check_same_thread=False)
cursor = conn.cursor()

cursor.execute('''
    CREATE TABLE IF NOT EXISTS poker_games (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id TEXT,
        players TEXT,
        winner TEXT,
        pot INTEGER,
        hands TEXT,
        created_at TEXT
    )
''')
conn.commit()

# ======================================
# ==== КЛАССЫ ПОКЕРА ===================
# ======================================

class Card:
    def __init__(self, suit, rank):
        self.suit = suit  # ♠ ♥ ♦ ♣
        self.rank = rank  # 2-14 (11=J,12=Q,13=K,14=A)
    
    def __str__(self):
        suits = {'♠': '♠', '♥': '♥', '♦': '♦', '♣': '♣'}
        ranks = {11: 'J', 12: 'Q', 13: 'K', 14: 'A'}
        rank_str = ranks.get(self.rank, str(self.rank))
        return f"{rank_str}{self.suit}"
    
    def to_dict(self):
        return {"suit": self.suit, "rank": self.rank}

class Deck:
    def __init__(self):
        self.cards = []
        suits = ['♠', '♥', '♦', '♣']
        for suit in suits:
            for rank in range(2, 15):  # 2-14
                self.cards.append(Card(suit, rank))
        self.shuffle()
    
    def shuffle(self):
        random.shuffle(self.cards)
    
    def deal(self, num=1):
        cards = []
        for _ in range(num):
            if self.cards:
                cards.append(self.cards.pop())
        return cards

class Hand:
    def __init__(self, cards):
        self.cards = cards
    
    def evaluate(self):
        """Оценка силы руки (упрощённо)"""
        if not self.cards:
            return 0
        
        # Простая оценка: сумма рангов + бонус за комбинации
        ranks = [c.rank for c in self.cards]
        suits = [c.suit for c in self.cards]
        
        # Проверка на флеш
        flush = len(set(suits)) == 1
        
        # Проверка на стрит
        ranks.sort()
        straight = all(ranks[i] + 1 == ranks[i + 1] for i in range(len(ranks) - 1))
        
        # Базовый счёт
        score = sum(ranks)
        
        if flush:
            score += 1000
        if straight:
            score += 500
        if flush and straight and max(ranks) == 14:  # Роял флеш
            score += 10000
        
        return score

class PokerPlayer:
    def __init__(self, user_id, name, stack=1000):
        self.user_id = user_id
        self.name = name
        self.stack = stack
        self.hand = []
        self.bet = 0
        self.is_active = True
        self.is_folded = False
        self.is_all_in = False
        self.seat = None
        self.connection = None
    
    def to_dict(self):
        return {
            "user_id": self.user_id,
            "name": self.name,
            "stack": self.stack,
            "bet": self.bet,
            "is_active": self.is_active,
            "is_folded": self.is_folded,
            "is_all_in": self.is_all_in,
            "seat": self.seat,
            "hand": [str(c) for c in self.hand] if self.hand else []
        }

class PokerTable:
    def __init__(self, table_id, max_players=6, small_blind=10, big_blind=20):
        self.table_id = table_id
        self.max_players = max_players
        self.players = []
        self.seats = [None] * max_players
        self.deck = Deck()
        self.community_cards = []
        self.pot = 0
        self.current_bet = 0
        self.current_player_index = 0
        self.dealer_index = 0
        self.small_blind = small_blind
        self.big_blind = big_blind
        self.round = 'waiting'  # waiting, preflop, flop, turn, river, showdown
        self.min_raise = big_blind
        self.last_raise = 0
        self.connections = []
    
    def add_player(self, player):
        if len(self.players) < self.max_players:
            # Находим свободное место
            for i in range(self.max_players):
                if self.seats[i] is None:
                    player.seat = i
                    self.seats[i] = player
                    self.players.append(player)
                    return True
        return False
    
    def remove_player(self, user_id):
        for i, player in enumerate(self.players):
            if player.user_id == user_id:
                self.seats[player.seat] = None
                self.players.pop(i)
                return True
        return False
    
    def start_game(self):
        if len(self.players) < 2:
            return False
        
        self.round = 'preflop'
        self.deck = Deck()
        self.community_cards = []
        self.pot = 0
        self.current_bet = 0
        
        # Сброс состояния игроков
        for player in self.players:
            player.hand = []
            player.bet = 0
            player.is_folded = False
            player.is_all_in = False
            player.is_active = True
        
        # Раздача карт
        for _ in range(2):
            for player in self.players:
                card = self.deck.deal()[0]
                player.hand.append(card)
        
        # Блайнды
        sb_index = (self.dealer_index + 1) % len(self.players)
        bb_index = (self.dealer_index + 2) % len(self.players)
        
        sb_player = self.players[sb_index]
        bb_player = self.players[bb_index]
        
        # Ставки блайндов
        sb_player.bet = min(sb_player.stack, self.small_blind)
        sb_player.stack -= sb_player.bet
        self.pot += sb_player.bet
        
        bb_player.bet = min(bb_player.stack, self.big_blind)
        bb_player.stack -= bb_player.bet
        self.pot += bb_player.bet
        
        self.current_bet = self.big_blind
        self.current_player_index = (bb_index + 1) % len(self.players)
        
        return True
    
    def next_round(self):
        if self.round == 'preflop':
            self.round = 'flop'
            for _ in range(3):
                card = self.deck.deal()[0]
                self.community_cards.append(card)
        elif self.round == 'flop':
            self.round = 'turn'
            card = self.deck.deal()[0]
            self.community_cards.append(card)
        elif self.round == 'turn':
            self.round = 'river'
            card = self.deck.deal()[0]
            self.community_cards.append(card)
        elif self.round == 'river':
            self.round = 'showdown'
            return self.showdown()
        
        self.current_bet = 0
        for player in self.players:
            if not player.is_folded and player.stack > 0:
                player.is_active = True
            player.bet = 0
        
        # Следующий игрок после дилера
        self.current_player_index = (self.dealer_index + 1) % len(self.players)
        while self.players[self.current_player_index].is_folded:
            self.current_player_index = (self.current_player_index + 1) % len(self.players)
        
        return None
    
    def showdown(self):
        # Определяем победителя
        active_players = [p for p in self.players if not p.is_folded]
        
        if len(active_players) == 1:
            return active_players[0]
        
        best_score = -1
        winners = []
        
        for player in active_players:
            all_cards = player.hand + self.community_cards
            hand = Hand(all_cards)
            score = hand.evaluate()
            
            if score > best_score:
                best_score = score
                winners = [player]
            elif score == best_score:
                winners.append(player)
        
        # Делим пот
        if winners:
            win_amount = self.pot // len(winners)
            for winner in winners:
                winner.stack += win_amount
        
        self.dealer_index = (self.dealer_index + 1) % len(self.players)
        
        return winners
    
    def place_bet(self, player, amount):
        if player.is_folded or not player.is_active:
            return False
        
        if amount > player.stack:
            amount = player.stack
            player.is_all_in = True
        
        player.stack -= amount
        player.bet += amount
        self.pot += amount
        
        if amount > self.current_bet - player.bet + amount:
            self.current_bet = player.bet
            self.last_raise = amount
        
        if player.stack == 0:
            player.is_all_in = True
        
        return True
    
    def fold(self, player):
        player.is_folded = True
        player.is_active = False
    
    def check(self, player):
        if player.bet < self.current_bet:
            return False
        player.is_active = False
        return True
    
    def call(self, player):
        amount = self.current_bet - player.bet
        return self.place_bet(player, amount)
    
    def raise_bet(self, player, amount):
        if amount < self.min_raise:
            return False
        return self.place_bet(player, amount)
    
    def next_player(self):
        if all(p.is_folded or not p.is_active or p.is_all_in for p in self.players):
            return self.next_round()
        
        next_index = (self.current_player_index + 1) % len(self.players)
        while self.players[next_index].is_folded or self.players[next_index].is_all_in:
            next_index = (next_index + 1) % len(self.players)
            if next_index == self.current_player_index:
                return self.next_round()
        
        self.current_player_index = next_index
        return None
    
    def get_state(self):
        return {
            "table_id": self.table_id,
            "players": [p.to_dict() for p in self.players],
            "community_cards": [str(c) for c in self.community_cards],
            "pot": self.pot,
            "current_bet": self.current_bet,
            "current_player_index": self.current_player_index,
            "dealer_index": self.dealer_index,
            "round": self.round,
            "min_raise": self.min_raise,
            "seats": [s.to_dict() if s else None for s in self.seats]
        }

# ======================================
# ==== МЕНЕДЖЕР СТОЛОВ =================
# ======================================

class PokerManager:
    def __init__(self):
        self.tables = {}
        self.players = {}  # user_id -> (table_id, player)
        self.waiting_queue = []
    
    def create_table(self, table_id=None):
        if table_id is None:
            table_id = f"table_{len(self.tables) + 1}"
        table = PokerTable(table_id)
        self.tables[table_id] = table
        return table
    
    def find_or_create_table(self):
        # Ищем стол с местом
        for table_id, table in self.tables.items():
            if len(table.players) < table.max_players:
                return table
        
        # Создаём новый
        return self.create_table()
    
    def add_player(self, user_id, name, connection=None):
        # Проверяем, есть ли уже игрок
        if user_id in self.players:
            return self.players[user_id]
        
        # Ищем или создаём стол
        table = self.find_or_create_table()
        
        player = PokerPlayer(user_id, name)
        player.connection = connection
        
        if table.add_player(player):
            self.players[user_id] = (table.table_id, player)
            return table
        return None
    
    def remove_player(self, user_id):
        if user_id in self.players:
            table_id, player = self.players[user_id]
            table = self.tables.get(table_id)
            if table:
                table.remove_player(user_id)
            del self.players[user_id]
    
    def get_table_by_player(self, user_id):
        if user_id in self.players:
            table_id, _ = self.players[user_id]
            return self.tables.get(table_id)
        return None

poker_manager = PokerManager()

# ======================================
# ==== WEBSOCKET КОМНАТЫ ===============
# ======================================

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
    
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
    
    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)
    
    async def send_personal_message(self, message: dict, websocket: WebSocket):
        await websocket.send_json(message)
    
    async def broadcast_to_table(self, table_id: str, message: dict, exclude=None):
        table = poker_manager.tables.get(table_id)
        if not table:
            return
        
        for player in table.players:
            if player.connection and player.connection != exclude:
                try:
                    await player.connection.send_json(message)
                except:
                    pass

manager = ConnectionManager()

@app.websocket("/poker/{user_id}")
async def poker_websocket(websocket: WebSocket, user_id: str):
    await manager.connect(websocket)
    
    try:
        # Получаем данные пользователя
        data = await websocket.receive_json()
        name = data.get('name', f'Player {user_id[:4]}')
        
        # Добавляем игрока в покер
        table = poker_manager.add_player(user_id, name, websocket)
        
        if not table:
            await websocket.send_json({"error": "No table available"})
            return
        
        # Отправляем состояние стола
        await websocket.send_json({
            "type": "table_state",
            "data": table.get_state()
        })
        
        # Уведомляем остальных
        await manager.broadcast_to_table(table.table_id, {
            "type": "player_joined",
            "data": table.get_state()
        }, websocket)
        
        # Основной цикл обработки сообщений
        while True:
            data = await websocket.receive_json()
            action = data.get('action')
            
            table = poker_manager.get_table_by_player(user_id)
            if not table:
                await websocket.send_json({"error": "Table not found"})
                continue
            
            # Находим игрока
            player = None
            for p in table.players:
                if p.user_id == user_id:
                    player = p
                    break
            
            if not player:
                continue
            
            # Обработка действий
            if action == 'start_game':
                if table.start_game():
                    await manager.broadcast_to_table(table.table_id, {
                        "type": "game_started",
                        "data": table.get_state()
                    })
            
            elif action == 'fold':
                table.fold(player)
                table.next_player()
                await manager.broadcast_to_table(table.table_id, {
                    "type": "player_folded",
                    "data": table.get_state()
                })
            
            elif action == 'check':
                if table.check(player):
                    table.next_player()
                    await manager.broadcast_to_table(table.table_id, {
                        "type": "player_checked",
                        "data": table.get_state()
                    })
            
            elif action == 'call':
                if table.call(player):
                    table.next_player()
                    await manager.broadcast_to_table(table.table_id, {
                        "type": "player_called",
                        "data": table.get_state()
                    })
            
            elif action == 'raise':
                amount = data.get('amount', 0)
                if table.raise_bet(player, amount):
                    table.next_player()
                    await manager.broadcast_to_table(table.table_id, {
                        "type": "player_raised",
                        "data": table.get_state()
                    })
            
            elif action == 'get_state':
                await websocket.send_json({
                    "type": "table_state",
                    "data": table.get_state()
                })
    
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        poker_manager.remove_player(user_id)
        
        # Уведомляем остальных
        for table_id, table in poker_manager.tables.items():
            for player in table.players:
                if player.connection:
                    try:
                        await player.connection.send_json({
                            "type": "player_left",
                            "data": {"user_id": user_id}
                        })
                    except:
                        pass

@app.get("/poker/tables")
def get_tables():
    return {
        "tables": [
            {
                "id": table_id,
                "players": len(table.players),
                "max_players": table.max_players
            }
            for table_id, table in poker_manager.tables.items()
        ]
    }

@app.get("/poker/table/{table_id}")
def get_table(table_id: str):
    table = poker_manager.tables.get(table_id)
    if not table:
        return {"error": "Table not found"}
    return table.get_state()

@app.get("/")
def root():
    return {
        "status": "online",
        "service": "Poker Server",
        "tables": len(poker_manager.tables),
        "players": len(poker_manager.players)
    }
