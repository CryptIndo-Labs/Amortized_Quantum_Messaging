#ifndef INVENTORY_MANAGER_H
#define INVENTORY_MANAGER_H

#include "../common/common.h"
#include <map>
#include <list>
#include <string>
#include <iostream>
#include <algorithm>
#include <mutex>

using namespace std;
class InventoryManager
{
	private:
	map<int, MintedCoin> public_cache;
	list<int> lru_list;
	const size_t MAX_CACHE_BYTES=64*1024;
	
	map<int, string> private_vault;
	mutex inventory_mutex;
	size_t get_current_size()
	{
		size_t size=0;
		for(const auto& pair: public_cache)
		{
			size+=pair.second.public_key_hex.size();
			size+=pair.second.signature_hex.size();
			size+=sizeof(MintedCoin);
		}
		return size;
	}
	
	public:
	InventoryManager()
	{
	
	}
	
	void store_public_key(const MintedCoin& coin)
	{
		lock_guard<mutex> lock(inventory_mutex);
		if(public_cache.find(coin.key_id)!=public_cache.end())
		{
			lru_list.remove(coin.key_id);
			lru_list.push_front(coin.key_id);
			return;
		}
		public_cache[coin.key_id]=coin;
		lru_list.push_front(coin.key_id);
		garbage_collect();		
	}
	MintedCoin* get_best_key(const string& user_id, Coin coin_type)
	{
		lock_guard<mutex> lock(inventory_mutex);
		for(auto& pair:public_cache)
		{
			if(pair.second.user_id==user_id && pair.second.coin==coin_type)
			{
				lru_list.remove(pair.first);
				lru_list.push_front(pair.first);
				return &pair.second;
			}
		}
		return nullptr;
	}
	void garbage_collect()
	{
		while(get_current_size()>MAX_CACHE_BYTES && !lru_list.empty())
		{
			int key_to_remove=lru_list.back();
			lru_list.pop_back();
			MintedCoin& c=public_cache[key_to_remove];
			public_cache.erase(key_to_remove);
		}
	}
	
	void store_private_key(int key_id, const string& raw_sk)
	{
		lock_guard<mutex> lock(inventory_mutex);
		string encrypted_block="ENC_HW_"+raw_sk;
		private_vault[key_id]=encrypted_block;
	}
	
	string retrieve_and_burn(int key_id)
	{
		lock_guard<mutex> lock(inventory_mutex);
		if(private_vault.find(key_id)==private_vault.end())
		{
			return "";
		}
		string enc=private_vault[key_id];
		string decrypted_sk="";
		if(enc.find("ENC_HW_")==0)
		{
			decrypted_sk=enc.substr(7);
		}
		private_vault.erase(key_id);
		return decrypted_sk;
	}
};

#endif
