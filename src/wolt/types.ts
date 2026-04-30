// src/wolt/types.ts

export interface Venue {
  platform: 'wolt';
  name: string;
  slug: string;
  oid: string;
  rating: number;
  delivery_time: number; // estimated minutes
  delivery_price: number; // EUR
  url: string;
}

export interface MenuItem {
  item_id: string;
  item_name: string;
  description: string;
  price: number; // EUR
  image: string;
  category: string;
}

export interface SearchItem {
  platform: 'wolt';
  item_name: string;
  venue_name: string;
  venue_slug: string;
  item_id: string;
  price: number;
  delivery_time: number;
  delivery_price?: number;
  url: string;
}

export interface DeliveryAddress {
  id?: string;
  formatted_address: string;
  lat?: number;
  lon?: number;
}

export interface CartItem {
  item_id: string;
  count: number;
  modifiers?: unknown[];
}

export interface OrderResult {
  success: boolean;
  order_id?: string;
  message: string;
}
