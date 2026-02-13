#ifndef CRYPTO_ENGINE_H
#define CRYPTO_ENGINE_H

#include <string>
#include <vector>
#include "../common/common.h"

using namespace std;

class CryptoEngine
{
	public:
	CryptoEngine();
	pair<string, string> generate_keypair(Coin coin);
	string sign_key(const string& pk_hex, Coin coin);	
	static string to_hex(const vector<uint8_t>& data);
};

#endif
